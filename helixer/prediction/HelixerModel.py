from abc import ABC, abstractmethod
import os
import sys
try:
    import nni
except ImportError:
    pass
import time
import h5py
import numcodecs
import argparse
import datetime
import pkg_resources
import subprocess
import numpy as np
import tensorflow as tf
from sklearn.utils import shuffle
from pprint import pprint

from keras_layer_normalization import LayerNormalization
from tensorflow.keras.callbacks import Callback
from tensorflow.keras import optimizers
from tensorflow.keras import backend as K
from tensorflow.keras.models import load_model
from tensorflow.keras.utils import Sequence

from helixer.prediction.ConfusionMatrix import ConfusionMatrix


class SaveEveryEpoch(Callback):
    def __init__(self, output_dir):
        super(SaveEveryEpoch, self).__init__()
        self.output_dir = output_dir

    def on_epoch_end(self, epoch, _):
        path = os.path.join(self.output_dir, f'model{epoch}.h5')
        self.model.save(path, save_format='h5')
        print(f'saved model at {path}')


class ConfusionMatrixTrain(Callback):
    def __init__(self, save_model_path, val_generator, patience, report_to_nni=False):
        self.save_model_path = save_model_path
        self.val_generator = val_generator
        self.patience = patience
        self.report_to_nni = report_to_nni
        self.best_val_genic_f1 = 0.0
        self.epochs_without_improvement = 0

    def on_epoch_begin(self, epoch, logs=None):
        self.epoch_start = time.time()

    def on_epoch_end(self, epoch, logs=None):
        print(f'training took {(time.time() - self.epoch_start) / 60:.2f}m')
        val_genic_f1 = HelixerModel.run_confusion_matrix(self.val_generator, self.model)
        if self.report_to_nni:
            nni.report_intermediate_result(val_genic_f1)
        if val_genic_f1 > self.best_val_genic_f1:
            self.best_val_genic_f1 = val_genic_f1
            self.model.save(self.save_model_path, save_format='h5')
            print('saved new best model with genic f1 of {} at {}'.format(self.best_val_genic_f1,
                                                                          self.save_model_path))
            self.epochs_without_improvement = 0
        else:
            self.epochs_without_improvement += 1
            if self.epochs_without_improvement >= self.patience:
                self.model.stop_training = True
        # hard-coded check of genic f1 of 0.6 at epoch 5
        if epoch == 5 and val_genic_f1 < 0.6:
            self.model.stop_training = True

    def on_train_end(self, logs=None):
        if self.report_to_nni:
            nni.report_final_result(self.best_val_genic_f1)


class PreshuffleCallback(Callback):
    def __init__(self, train_generator):
        self.train_generator = train_generator

    def on_epoch_begin(self, epoch, logs=None):
        if self.train_generator.shuffle:
            self.train_generator.shuffle_data()


class HelixerSequence(Sequence):
    def __init__(self, model, h5_file, mode, batch_size, shuffle):
        assert mode in ['train', 'val', 'test']
        self.model = model
        self.h5_file = h5_file
        self.mode = mode
        self.shuffle = shuffle
        self.batch_size = batch_size
        self._cp_into_namespace(['float_precision', 'class_weights', 'transition_weights', 'input_coverage',
                                 'stretch_transition_weights', 'coverage_weights', 'coverage_offset', 'debug'])
        x_dset, y_dset = h5_file['data/X'], h5_file['data/y']
        if self.debug:
            self.n_seqs = 10000
        else:
            self.n_seqs = y_dset.shape[0]
        self.chunk_size = y_dset.shape[1]

        print(f'\nStarting to load {self.mode} data into memory..')
        print(f'X shape: {x_dset.shape}')
        print(f'y shape: {y_dset.shape}')

        self.data_list_names = ['data/X', 'data/y', 'data/sample_weights']
        if self.mode == 'train':
            if self.transition_weights is not None:
                self.data_list_names.append('data/transitions')
            if self.coverage_weights:
                self.data_list_names.append('scores/by_bp')

        if self.input_coverage:
            self.data_list_names += ['evaluation/coverage', 'evaluation/spliced_coverage']

        self.data_lists = [[] for _ in range(len(self.data_list_names))]
        self.data_dtypes = [h5_file[name].dtype for name in self.data_list_names]

        self.compressor = numcodecs.blosc.Blosc(cname='blosclz', clevel=4, shuffle=2)  # use BITSHUFFLE

        # load at most 10000 uncompressed samples at a time in memory
        for name, data_list in zip(self.data_list_names, self.data_lists):
            start_time_dset = time.time()
            for offset in range(0, self.n_seqs, 10000):
                data_slice = h5_file[name][offset:offset + 10000]
                data_list.extend([self.compressor.encode(e) for e in data_slice])
            print(f'Data loading of {len(data_list)} samples of {name} into memory took '
                  f'{time.time() - start_time_dset:.2f} secs')
            comp_data_size = sum([sys.getsizeof(e) for e in data_list])
            print(f'Compressed data size of {name} is at least {comp_data_size / 2 ** 30:.4f} GB\n')

    def shuffle_data(self):
        start_time = time.time()
        self.data_lists = shuffle(*self.data_lists)
        print(f'Reshuffled {self.mode} data in {time.time() - start_time:.2f} secs')

    def _cp_into_namespace(self, names):
        """Moves class properties from self.model into this class for brevity"""
        for name in names:
            self.__dict__[name] = self.model.__dict__[name]

    def _get_batch_data(self, idx):
        batch = []
        # batch must have one thing for everything unpacked by __getitem__ (and in order)
        for name in ['data/X', 'data/y', 'data/sample_weights', 'data/transitions', 'scores/by_bp']:
            if name not in self.data_list_names:
                batch.append(None)
            else:
                decoded_list = self._decode_one(name, idx)
                # append coverage to X directly, might be clearer elsewhere once working, but this needs little code...
                if name == 'data/X' and self.input_coverage:
                    decode_coverage = self._decode_one('evaluation/coverage', idx)
                    decode_coverage = [np.log(x.reshape(-1, 1) + 1.1).astype(np.float16) for x in decode_coverage]
                    decode_spliced = self._decode_one('evaluation/spliced_coverage', idx)
                    decode_spliced = [np.log(x.reshape(-1, 1) + 1.1).astype(np.float16) for x in decode_spliced]
                    decoded_list = [np.concatenate((x, y, z), axis=1) for x, y, z in
                                    zip(decoded_list, decode_coverage, decode_spliced)]
                batch.append(np.stack(decoded_list, axis=0))

        return tuple(batch)

    def _decode_one(self, name, idx):
        """decode position {idx} from compressed data originally from dataset {name}"""
        i = self.data_list_names.index(name)
        dtype = self.data_dtypes[i]
        data_list = self.data_lists[i]
        decoded_list = [np.frombuffer(self.compressor.decode(e), dtype=dtype)
                        for e in data_list[idx * self.batch_size:(idx + 1) * self.batch_size]]
        if len(decoded_list[0]) > self.chunk_size:
            decoded_list = [e.reshape(self.chunk_size, -1) for e in decoded_list]
        return decoded_list

    def _update_sw_with_transition_weights(self):
        pass

    def _update_sw_with_coverage_weights(self):
        pass

    def _mk_timestep_pools(self, matrix):
        """reshape matrix to have multiple bp per timestep (in last dim)"""
        # assumes input shape
        # [0] = batch_size            --> don't touch
        # [1] = data's chunk_size     --> divide by pool size
        # [2:] = collapsable          --> -1, remaining, AKA np.prod(shape[2:]) * pool_size
        pool_size = self.model.pool_size
        if matrix is None:
            return None
        shape = list(matrix.shape)
        shape[1] = shape[1] // pool_size
        shape[-1] = -1
        matrix = matrix.reshape((
            shape
        ))
        return matrix

    def _mk_timestep_pools_class_last(self, matrix):
        """reshape matrix to have multiple bp per timestep, w/ classes as last dim for softmax"""
        if matrix is None:
            return None
        pool_size = self.model.pool_size
        assert len(matrix.shape) == 3
        # assumes input shape
        # [0] = batch_size            --> don't touch
        # [1] = data's chunk_size     --> divide by pool size
        # [2] = labels                --> pooling inserted before, retained as last dimension
        matrix = matrix.reshape((
            matrix.shape[0],
            matrix.shape[1] // pool_size,
            pool_size,  # make labels 2d so we can use the standard softmax / loss functions
            matrix.shape[-1],
        ))
        return matrix

    def _aggregate_timestep_pools(self, matrix, aggr_function=np.mean):
        pass

    def compress_tw(self, transitions):
        return self._squish_tw_to_sw(transitions, self.transition_weights, self.stretch_transition_weights)

    # todo, make the following more generic? or naming more consistent?
    @staticmethod
    def _squish_tw_to_sw(transitions, tw, stretch):
        sw_t = [np.any((transitions[:, :, :, col] == 1), axis=2) for col in range(6)]
        sw_t = np.stack(sw_t, axis=2).astype(np.int8)
        sw_t = np.multiply(sw_t, tw)

        sw_t = np.sum(sw_t, axis=2)
        where_are_ones = np.where(sw_t == 0)
        sw_t[where_are_ones[0], where_are_ones[1]] = 1
        if stretch != 0:
            sw_t = HelixerSequence._apply_stretch(sw_t, stretch)
        return sw_t

    @staticmethod
    def _apply_stretch(reshaped_sw_t, stretch):
        """modifies sample weight shaped transitions so they are a peak instead of a single point"""
        reshaped_sw_t = np.array(reshaped_sw_t)
        dilated_rf = np.ones(np.shape(reshaped_sw_t))

        where = np.where(reshaped_sw_t > 1)
        i = np.array(where[0])  # i unchanged
        j = np.array(where[1])  # j +/- step

        # find dividers depending on the size of the dilated rf
        dividers = []
        for distance in range(1, stretch + 1):
            dividers.append(2**distance)

        for z in range(stretch, 0, -1):
            dilated_rf[i, np.maximum(np.subtract(j, z), 0)] = np.maximum(reshaped_sw_t[i, j]/dividers[z-1], 1)
            dilated_rf[i, np.minimum(np.add(j, z), len(dilated_rf[0])-1)] = np.maximum(reshaped_sw_t[i, j]/dividers[z-1], 1)
        dilated_rf[i, j] = np.maximum(reshaped_sw_t[i, j], 1)
        return dilated_rf

    def __len__(self):
        return int(np.ceil(self.n_seqs / self.batch_size))

    @abstractmethod
    def __getitem__(self, idx):
        pass


class HelixerModel(ABC):
    def __init__(self):
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument('-d', '--data-dir', type=str, default='')
        self.parser.add_argument('-s', '--save-model-path', type=str, default='./best_model.h5')
        # training params
        self.parser.add_argument('-e', '--epochs', type=int, default=10000)
        self.parser.add_argument('-b', '--batch-size', type=int, default=8)
        self.parser.add_argument('--val-test-batch-size', type=int, default=8)
        self.parser.add_argument('--loss', type=str, default='')
        self.parser.add_argument('--patience', type=int, default=3)
        self.parser.add_argument('--optimizer', type=str, default='adam')
        self.parser.add_argument('--clip-norm', type=float, default=1.0)
        self.parser.add_argument('--learning-rate', type=float, default=3e-4)
        self.parser.add_argument('--class-weights', type=str, default='None')
        self.parser.add_argument('--input-coverage', action='store_true')
        self.parser.add_argument('--transition-weights', type=str, default='None')
        self.parser.add_argument('--stretch-transition-weights', type=int, default=0)
        self.parser.add_argument('--coverage-weights', action='store_true')
        self.parser.add_argument('--coverage-offset', type=float, default=0.0)
        self.parser.add_argument('--resume-training', action='store_true')
        # testing
        self.parser.add_argument('-l', '--load-model-path', type=str, default='')
        self.parser.add_argument('-t', '--test-data', type=str, default='')
        self.parser.add_argument('-p', '--prediction-output-path', type=str, default='predictions.h5')
        self.parser.add_argument('--eval', action='store_true')
        # resources
        self.parser.add_argument('--float-precision', type=str, default='float32')
        self.parser.add_argument('--cpus', type=int, default=8)
        self.parser.add_argument('--gpu-id', type=int, default=-1)
        self.parser.add_argument('--workers', type=int, default=1,
                                 help='Probaly should be the same a number of GPUs')
        # misc flags
        self.parser.add_argument('--save-every-epoch', action='store_true')
        self.parser.add_argument('--nni', action='store_true')
        self.parser.add_argument('-v', '--verbose', action='store_true')
        self.parser.add_argument('--debug', action='store_true')
        self.parser.add_argument('--progbar', action='store_true')
        self.parser.add_argument('--tf-errors', action='store_true')

    def parse_args(self):
        args = vars(self.parser.parse_args())
        self.__dict__.update(args)
        self.testing = bool(self.load_model_path and not self.resume_training)
        assert not (self.testing and self.data_dir)
        assert not (not self.testing and self.test_data)
        assert not (self.resume_training and (not self.load_model_path or not self.data_dir))

        if self.nni:
            hyperopt_args = nni.get_next_parameter()
            assert all([key in args for key in hyperopt_args.keys()]), 'Unknown nni parameter'
            self.__dict__.update(hyperopt_args)
            nni_save_model_path = os.path.expandvars('$NNI_OUTPUT_DIR/best_model.h5')
            nni_pred_output_path = os.path.expandvars('$NNI_OUTPUT_DIR/predictions.h5')
            self.__dict__['save_model_path'] = nni_save_model_path
            self.__dict__['prediction_output_path'] = nni_pred_output_path
            args.update(hyperopt_args)
            # for the print out
            args['save_model_path'] = nni_save_model_path
            args['prediction_output_path'] = nni_pred_output_path

        self.class_weights = eval(self.class_weights)
        if type(self.class_weights) is list:
            self.class_weights = np.array(self.class_weights, dtype=np.float32)

        self.transition_weights = eval(self.transition_weights)
        if type(self.transition_weights) is list:
            self.transition_weights = np.array(self.transition_weights, dtype = np.float32)

        if self.verbose:
            print()
            pprint(args)

    def generate_callbacks(self, train_generator):
        callbacks = [ConfusionMatrixTrain(self.save_model_path, self.gen_validation_data(),
                                          self.patience, report_to_nni=self.nni)]
        callbacks.append(PreshuffleCallback(train_generator))
        if self.save_every_epoch:
            callbacks.append(SaveEveryEpoch(os.path.dirname(self.save_model_path)))
        return callbacks

    def set_resources(self):
        config = tf.compat.v1.ConfigProto()
        config.gpu_options.allow_growth = True  # dynamically grow the memory used on the GPU

        K.set_floatx(self.float_precision)
        if self.gpu_id > -1:
            os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
            os.environ['CUDA_VISIBLE_DEVICES'] = str(self.gpu_id)

    def gen_training_data(self):
        SequenceCls = self.sequence_cls()
        return SequenceCls(model=self, h5_file=self.h5_train, mode='train', batch_size=self.batch_size,
                           shuffle=True)

    def gen_validation_data(self):
        SequenceCls = self.sequence_cls()
        return SequenceCls(model=self, h5_file=self.h5_val, mode='val', batch_size=self.val_test_batch_size,
                           shuffle=False)

    def gen_test_data(self):
        SequenceCls = self.sequence_cls()
        return SequenceCls(model=self, h5_file=self.h5_test, mode='test', batch_size=self.val_test_batch_size,
                           shuffle=False)

    @staticmethod
    def run_confusion_matrix(generator, model):
        start = time.time()
        cm_calculator = ConfusionMatrix(generator)
        genic_f1 = cm_calculator.calculate_cm(model)
        if np.isnan(genic_f1):
            genic_f1 = 0.0
        print('\ncm calculation took: {:.2f} minutes\n'.format(int(time.time() - start) / 60))
        return genic_f1

    @abstractmethod
    def sequence_cls(self):
        pass

    @abstractmethod
    def model(self):
        pass

    @abstractmethod
    def compile_model(self, model):
        pass

    def plot_model(self, model):
        from tensorflow.keras.utils import plot_model
        plot_model(model, to_file='model.png')
        print('Plotted to model.png')
        sys.exit()

    def open_data_files(self):
        def get_n_correct_seqs(h5_file):
            err_samples = np.array(h5_file['/data/err_samples'])
            n_correct = np.count_nonzero(err_samples == False)
            if n_correct == 0:
                print('WARNING: no fully correct sample found')
            return n_correct

        def get_n_intergenic_seqs(h5_file):
            ic_samples = np.array(h5_file['/data/fully_intergenic_samples'])
            n_fully_ig = np.count_nonzero(ic_samples == True)
            if n_fully_ig == 0:
                print('WARNING: no fully intergenic samples found')
            return n_fully_ig

        if not self.testing:
            self.h5_train = h5py.File(os.path.join(self.data_dir, 'training_data.h5'), 'r')
            self.h5_val = h5py.File(os.path.join(self.data_dir, 'validation_data.h5'), 'r')
            self.shape_train = self.h5_train['/data/X'].shape
            self.shape_val = self.h5_val['/data/X'].shape

            n_train_correct_seqs = get_n_correct_seqs(self.h5_train)
            n_val_correct_seqs = get_n_correct_seqs(self.h5_val)

            n_train_seqs = self.shape_train[0]
            n_val_seqs = self.shape_val[0]  # always validate on all

            n_intergenic_train_seqs = get_n_intergenic_seqs(self.h5_train)
            n_intergenic_val_seqs = get_n_intergenic_seqs(self.h5_val)
        else:
            self.h5_test = h5py.File(self.test_data, 'r')
            self.shape_test = self.h5_test['/data/X'].shape

            n_test_correct_seqs = get_n_correct_seqs(self.h5_test)
            n_test_seqs_with_intergenic = self.shape_test[0]
            n_intergenic_test_seqs = get_n_intergenic_seqs(self.h5_test)

        if self.verbose:
            print('\nData config: ')
            if not self.testing:
                print(dict(self.h5_train.attrs))
                print('\nTraining data shape: {}'.format(self.shape_train[:2]))
                print('Validation data shape: {}'.format(self.shape_val[:2]))
                print('\nTotal est. training sequences: {}'.format(n_train_seqs))
                print('Total est. val sequences: {}'.format(n_val_seqs))
                print('\nEst. intergenic train/val seqs: {:.2f}% / {:.2f}%'.format(
                    n_intergenic_train_seqs / n_train_seqs * 100,
                    n_intergenic_val_seqs / n_val_seqs * 100))
                print('Fully correct train/val seqs: {:.2f}% / {:.2f}%\n'.format(
                    n_train_correct_seqs / self.shape_train[0] * 100,
                    n_val_correct_seqs / self.shape_val[0] * 100))
            else:
                print(dict(self.h5_test.attrs))
                print('\nTest data shape: {}'.format(self.shape_test[:2]))
                print('\nIntergenic test seqs: {:.2f}%'.format(
                    n_intergenic_test_seqs / n_test_seqs_with_intergenic * 100))
                print('Fully correct test seqs: {:.2f}%\n'.format(
                    n_test_correct_seqs / self.shape_test[0] * 100))

    def _make_predictions(self, model):
        # loop through batches and continuously expand output dataset as everything might
        # not fit in memory
        pred_out = h5py.File(self.prediction_output_path, 'w')
        test_sequence = self.gen_test_data()

        for i in range(len(test_sequence)):
            if self.verbose:
                print(i, '/', len(test_sequence), end='\r')
            predictions = model.predict_on_batch(test_sequence[i][0])
            # join last two dims when predicting one hot labels
            predictions = predictions.reshape(predictions.shape[:2] + (-1,))
            # reshape when predicting more than one point at a time
            label_dim = 4
            if predictions.shape[2] != label_dim:
                n_points = predictions.shape[2] // label_dim
                predictions = predictions.reshape(
                    predictions.shape[0],
                    predictions.shape[1] * n_points,
                    label_dim,
                )
                # add 0-padding if needed
                n_removed = self.shape_test[1] - predictions.shape[1]
                if n_removed > 0:
                    zero_padding = np.zeros((predictions.shape[0], n_removed, predictions.shape[2]),
                                            dtype=predictions.dtype)
                    predictions = np.concatenate((predictions, zero_padding), axis=1)
            else:
                n_removed = 0  # just to avoid crashing with Unbound Local Error setting attrs for dCNN

            # prepare h5 dataset and save the predictions to disk
            if i == 0:
                old_len = 0
                pred_out.create_dataset('/predictions',
                                        data=predictions,
                                        maxshape=(None,) + predictions.shape[1:],
                                        chunks=(1,) + predictions.shape[1:],
                                        dtype='float32',
                                        compression='lzf',
                                        shuffle=True)
            else:
                old_len = pred_out['/predictions'].shape[0]
                pred_out['/predictions'].resize(old_len + predictions.shape[0], axis=0)
            pred_out['/predictions'][old_len:] = predictions

        # add model config and other attributes to predictions
        h5_model = h5py.File(self.load_model_path, 'r')
        pred_out.attrs['model_config'] = h5_model.attrs['model_config']
        pred_out.attrs['n_bases_removed'] = n_removed
        pred_out.attrs['test_data_path'] = self.test_data
        pred_out.attrs['model_path'] = self.load_model_path
        pred_out.attrs['timestamp'] = str(datetime.datetime.now())
        pred_out.attrs['model_md5sum'] = self.loaded_model_hash
        pred_out.close()
        h5_model.close()

    def _load_helixer_model(self):
        model = load_model(self.load_model_path, custom_objects={
            'LayerNormalization': LayerNormalization,
        })
        return model

    def _print_model_info(self, model):
        pwd = os.getcwd()
        os.chdir(os.path.dirname(__file__))
        try:
            cmd = ['git', 'rev-parse', '--abbrev-ref', 'HEAD']
            branch = subprocess.check_output(cmd, stderr=subprocess.STDOUT).strip().decode()
            cmd = ['git', 'describe', '--always']  # show tag or hash if no tag available
            commit = subprocess.check_output(cmd, stderr=subprocess.STDOUT).strip().decode()
            print(f'Current Helixer branch: {branch} ({commit})')
        except subprocess.CalledProcessError:
            version = pkg_resources.require('helixer')[0].version
            print(f'Current Helixer version: {version}')

        try:
            if os.path.isfile(self.load_model_path):
                cmd = ['md5sum', self.load_model_path]
                self.loaded_model_hash = subprocess.check_output(cmd).strip().decode()
                print(f'Md5sum of the loaded model: {self.loaded_model_hash}')
        except subprocess.CalledProcessError:
            print('An error occured while running a subprocess')
            self.loaded_model_hash = 'error'

        print()
        if self.verbose:
            print(model.summary())
        else:
            print('Total params: {:,}'.format(model.count_params()))
        os.chdir(pwd)  # return to previous directory

    def run(self):
        self.set_resources()
        self.open_data_files()
        # we either train or predict
        if not self.testing:
            if self.resume_training:
                model = self._load_helixer_model()
            else:
                model = self.model()
            self._print_model_info(model)

            if self.optimizer.lower() == 'adam':
                self.optimizer = optimizers.Adam(lr=self.learning_rate, clipnorm=self.clip_norm)
            elif self.optimizer.lower() == 'adabelief':
                from adabelief_tf import AdaBeliefOptimizer
                self.optimizer = AdaBeliefOptimizer(learning_rate=1e-3, epsilon=1e-16, rectify=False,
                                                    clipnorm=self.clip_norm)

            self.compile_model(model)

            train_generator = self.gen_training_data()
            model.fit(train_generator,
                      epochs=self.epochs,
                      workers=self.workers,
                      callbacks=self.generate_callbacks(train_generator),
                      verbose=self.progbar)
        else:
            assert self.test_data.endswith('.h5'), 'Need a h5 test data file when loading a model'
            assert self.load_model_path.endswith('.h5'), 'Need a h5 model file'

            model = self._load_helixer_model()
            self._print_model_info(model)

            if self.eval:
                _ = HelixerModel.run_confusion_matrix(self.gen_test_data(), model)
            else:
                if os.path.isfile(self.prediction_output_path):
                    print(f'{self.prediction_output_path} already exists and will be overwritten.')
                self._make_predictions(model)
            self.h5_test.close()