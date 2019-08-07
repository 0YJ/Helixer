#! /usr/bin/env python3
import h5py
import numpy as np
import argparse
import itertools

parser = argparse.ArgumentParser()
parser.add_argument('-d', '--data', type=str, required=True)
args = parser.parse_args()

binary_combs = list(itertools.product([0, 1], repeat=3))

f = h5py.File(args.data, 'r')
y = np.array(f['/data/y'])

uniques = np.unique(y.reshape(-1, y.shape[2]), return_counts=True, axis=0)
for row, count in zip(uniques[0], uniques[1]):
    print(row, count)


