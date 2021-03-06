import os
import random
import datetime
import time
import git

import nibabel as nib
import numpy as np
import yaml
import argparse
import itertools

from scipy.interpolate import RegularGridInterpolator

os.environ['PYTHONHASHSEED'] = '0'
np.random.seed(42)
random.seed(12345)


def interpolate(idx, dwi, block_size):

    if dwi.ndim == 3:
        dwi = dwi[:, :, :, np.newaxis]

    IDX = np.round(idx).astype(int)

    values = np.zeros([3, 3, 3,
                       block_size, block_size, block_size,
                       dwi.shape[-1]])

    for x in range(3):
        for y in range(3):
            for z in range(3):
                values[x, y, z,:] = dwi[
                    IDX[0] + x - 2 * (block_size // 2) : IDX[0] + x + 1,
                    IDX[1] + y - 2 * (block_size // 2) : IDX[1] + y + 1,
                    IDX[2] + z - 2 * (block_size // 2) : IDX[2] + z + 1,
                    :]

    fn = RegularGridInterpolator(
        ([-1,0,1],[-1,0,1],[-1,0,1]), values)
    
    return (fn([idx[0]-IDX[0], idx[1]-IDX[1], idx[2]-IDX[2]])[0]).flatten()


def generate_conditional_samples(fa_data,
                                 dwi,
                                 tracts,
                                 dwi_xyz2ijk,
                                 block_size,
                                 n_samples):

    fiber_lengths = [len(f) - 1 for f in tracts]
    n_samples = min(2*np.sum(fiber_lengths), n_samples)
    #===========================================================================
    inputs = np.zeros([n_samples, 3 + 1 + dwi.shape[-1] * block_size**3],
        dtype="float32")
    outgoing = np.zeros([n_samples, 3], dtype="float32")
    isterminal = np.zeros(n_samples, dtype="float32")
    FA = np.zeros(n_samples, dtype="float32")
    done=False
    n = 0
    for tract in tracts:
        last_pt = min(len(tract.streamline) - 1, (n_samples - n) // 2)
        for i, pt in enumerate(tract.streamline):
            #-------------------------------------------------------------------
            idx = dwi_xyz2ijk(pt)
            d = interpolate(idx, dwi, block_size)
            dnorm = np.linalg.norm(d)
            d /= (dnorm + 10**-2)
            #-------------------------------------------------------------------
            FA[n] = interpolate(idx, fa_data, 1)
            #-------------------------------------------------------------------
            if i == 0:
                vout = - tract.data_for_points["t"][i]
                vin = - tract.data_for_points["t"][i+1]
            else:
                vout = tract.data_for_points["t"][i]
                vin = tract.data_for_points["t"][i-1]
            inputs[n] = np.hstack([vin, d, dnorm])
            outgoing[n] = vout
            if i in [0, last_pt]:
                isterminal[n] = 1
            n += 1
            #-------------------------------------------------------------------
            if i not in [0, last_pt]:
                inputs[n] = np.hstack([-vin, d, dnorm])
                outgoing[n] = -vout
                n += 1
            #-------------------------------------------------------------------
            if n == n_samples:
                done = True
                break

        print("Finished {:3.0f}%".format(100*n/n_samples), end="\r")

        if done:
            return (n_samples,
                {"inputs": inputs, "isterminal": isterminal,
                 "outgoing": outgoing, "fa": FA})


def generate_prior_samples(dwi,
                           tracts,
                           dwi_xyz2ijk,
                           block_size,
                           n_samples):

    n_samples = min(2*len(tracts), n_samples)
    #===========================================================================
    inputs = np.zeros([n_samples, 1 + dwi.shape[-1] * block_size**3],
        dtype="float32")
    outgoing = np.zeros([n_samples, 3], dtype="float32")
    done=False
    n=0
    for tract in tracts:
        for i, pt in enumerate(tract.streamline[[0, -1]]):
            #-------------------------------------------------------------------
            idx = dwi_xyz2ijk(pt)
            d = interpolate(idx, dwi, block_size)
            dnorm = np.linalg.norm(d)
            d /= (dnorm + 10**-2)
            #-------------------------------------------------------------------
            vout = tract.data_for_points["t"][i]
            if i == 1:
                vout *= -1
            inputs[n] = np.hstack([d, dnorm])
            outgoing[n] = vout
            n += 1
            #-------------------------------------------------------------------
            if n == n_samples:
                done = True
                break
            #-------------------------------------------------------------------
        print("Finished {:3.0f}%".format(100*n/n_samples), end="\r")

        if done:
            return n_samples, {"inputs": inputs, "outgoing": outgoing}


def _sort_and_groupby(all_inputs, all_outputs, all_terminals):
    all_arrays = zip(all_inputs, all_outputs, all_terminals)
    sorted_arrays = sorted(all_arrays, key=lambda element: element[0].shape[0])
    group = itertools.groupby(sorted_arrays,
        key=lambda element: element[0].shape[0])

    inputs = []
    outs = []
    terminals = []
    for length, same_length_arrays in group:
        same_length_arrays1, same_length_arrays2, same_length_arrays3 = \
            itertools.tee(same_length_arrays, 3)
        inputs.append(np.concatenate([np.array(arr[0])[np.newaxis, :]
                                      for arr in same_length_arrays1], axis=0))
        outs.append(np.concatenate([np.array(arr[1])[np.newaxis, :]
                                    for arr in same_length_arrays2], axis=0))
        terminals.append(np.concatenate([np.array(arr[2])[np.newaxis, :]
                                         for arr in same_length_arrays3], axis=0))

    return inputs, outs, terminals


def generate_rnn_samples(dwi, tracts, dwi_xyz2ijk, block_size, n_samples):

    fiber_lengths = [len(f) - 1 for f in tracts]
    n_samples = min(2*np.sum(fiber_lengths), n_samples)

    #===========================================================================
    all_inputs = []
    all_outgoings = []
    all_isterminals = []
    done=False
    n = 0
    for tract in tracts:
        tract_n_samples = min((len(tract.streamline) - 1), (n_samples - n) // 2)

        inputs = np.zeros([tract_n_samples, 
            3 + 1 + dwi.shape[-1] * block_size ** 3], dtype="float32")
        outgoing = np.zeros([tract_n_samples, 3], dtype="float32")
        isterminal = np.zeros(tract_n_samples, dtype="float32")

        reverse_inputs = np.zeros([tract_n_samples, 
            3 + 1 + dwi.shape[-1] * block_size ** 3], dtype="float32")
        reverse_outgoing = np.zeros([tract_n_samples, 3], dtype="float32")
        reverse_isterminal = np.zeros(tract_n_samples, dtype="float32")

        last_pt = tract_n_samples
        for i, pt in enumerate(tract.streamline):
            #-------------------------------------------------------------------
            idx = dwi_xyz2ijk(pt)
            d = interpolate(idx, dwi, block_size)
            dnorm = np.linalg.norm(d)
            d /= (dnorm + 10**-2)
            #-------------------------------------------------------------------

            if i == 0:
                # First is only used as the end point of the reverse direction
                vout = - tract.data_for_points["t"][i]
                vin = - tract.data_for_points["t"][i + 1]

                reverse_inputs[tract_n_samples - i - 1] = np.hstack(
                    [-vin, d, dnorm])
                reverse_outgoing[tract_n_samples - i - 1] = -vout
                reverse_isterminal[tract_n_samples - i - 1] = 1
                n += 1

            elif i == last_pt:
                # Last is only used as the last point of usual direction
                vout = tract.data_for_points["t"][i]
                vin = tract.data_for_points["t"][i - 1]

                inputs[i - 1] = np.hstack([vin, d, dnorm])
                outgoing[i - 1] = vout
                isterminal[i - 1] = 1
                n += 1
            else:
                # Other points are used in both direction
                vout = tract.data_for_points["t"][i]
                vin = tract.data_for_points["t"][i - 1]

                inputs[i - 1] = np.hstack([vin, d, dnorm])
                outgoing[i - 1] = vout
                n += 1

                reverse_inputs[tract_n_samples - i - 1] = np.hstack(
                    [-vin, d, dnorm])
                reverse_outgoing[tract_n_samples - i - 1] = -vout
                n += 1



            #-------------------------------------------------------------------
            if n == n_samples:
                done = True
                break

        all_inputs.append(inputs)
        all_outgoings.append(outgoing)
        all_isterminals.append(isterminal)
        all_inputs.append(reverse_inputs)
        all_outgoings.append(reverse_outgoing)
        all_isterminals.append(reverse_isterminal)
        print("Finished {:3.0f}%".format(100*n/n_samples), end="\r")

        if done:
            start_time = time.time()
            print("Grouping and concatenating ...")
            all_inputs, all_outgoings, all_isterminals = _sort_and_groupby(
                all_inputs, all_outgoings, all_isterminals)
            print("Concatenation done in {0}".format(time.time() - start_time))
            return (n_samples,
                {"inputs": all_inputs, "isterminal": all_isterminals,
                 "outgoing": all_outgoings})


def generate_samples(dwi_path,
                     trk_path,
                     model,
                     block_size,
                     n_samples,
                     out_dir,
                     n_files):
    """"""
    assert n_samples % 2 == 0

    trk_file = nib.streamlines.load(trk_path)
    assert trk_file.tractogram.data_per_point is not None
    assert "t" in trk_file.tractogram.data_per_point
    #===========================================================================
    dwi_img = nib.load(dwi_path)
    dwi_img = nib.funcs.as_closest_canonical(dwi_img)
    dwi_aff = dwi_img.affine
    dwi_affi = np.linalg.inv(dwi_aff)
    dwi_xyz2ijk = lambda r: dwi_affi.dot([r[0], r[1], r[2], 1])[:3]
    dwi = dwi_img.get_data()

    fa_path = os.path.join(os.path.dirname(dwi_path), "tensor_FA.nii.gz")
    fa_img = nib.load(fa_path)
    fa_img = nib.funcs.as_closest_canonical(fa_img)
    fa_data = fa_img.get_data()

    tracts = trk_file.tractogram # fiber coordinates in rasmm
    #===========================================================================
    if model == "conditional":
        n_samples, samples = generate_conditional_samples(fa_data, dwi, tracts,
            dwi_xyz2ijk, block_size, n_samples)
    elif model == "prior":
        n_samples, samples = generate_prior_samples(dwi, tracts, dwi_xyz2ijk,
            block_size, n_samples)
    elif model == "RNN":
        n_samples, samples = generate_rnn_samples(dwi, tracts,
            dwi_xyz2ijk, block_size, n_samples)
    #===========================================================================
    if model != "RNN":
        np.random.seed(42)
        perm = np.random.permutation(n_samples)
        for k, v in samples.items():
            assert not np.isnan(v).any()
            assert not np.isinf(v).any()
            samples[k] = v[perm]
    #===========================================================================
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(dwi_path), "samples")
    out_dir = os.path.join(out_dir, timestamp)
    os.makedirs(out_dir, exist_ok=True)

    input_shape = ((1, samples["inputs"][0].shape[-1]) if model == 'RNN'
                   else samples["inputs"].shape[1:])
    if model == 'RNN':
        sample_path = os.path.join(out_dir, "samples-{0}.npz")
        for i in range(len(samples['inputs'])):
            print("Saving {}".format(sample_path.format(i)))
            sample_tosave = {'inputs': samples['inputs'][i],
                             'outgoing': samples['outgoing'][i],
                             'isterminal': samples['isterminal'][i]}
            np.savez(
                sample_path.format(i),
                input_shape=input_shape,
                sample_shape=sample_tosave['inputs'].shape,
                n_samples=n_samples,
                **sample_tosave)
    elif n_files != 1:
        sample_path = os.path.join(out_dir, "samples-{0}.npz")
        n_per_file = n_samples // n_files
        for i in range(n_files):
            print("Saving {}".format(sample_path.format(i)))

            if i == n_files - 1:
                sample_tosave = {'inputs': samples['inputs'][i * n_per_file:,...],
                                 'outgoing': samples['outgoing'][i * n_per_file:,...],
                                 'isterminal': samples['isterminal'][i * n_per_file:,...]}
            else:
                sample_tosave = {'inputs': samples['inputs'][i*n_per_file: (i+1)*n_per_file, ...],
                                 'outgoing': samples['outgoing'][i*n_per_file: (i+1)*n_per_file, ...],
                                 'isterminal': samples['isterminal'][i*n_per_file: (i+1)*n_per_file, ...]}

            np.savez(
                sample_path.format(i),
                input_shape=input_shape,
                sample_shape=sample_tosave['inputs'].shape,
                n_samples=n_samples,
                **sample_tosave)
    else:
        sample_path = os.path.join(out_dir, "samples.npz")
        print("\nSaving {}".format(sample_path))
        np.savez(
            sample_path,
            input_shape=input_shape,
            n_samples=n_samples,
            **samples)

    repo = git.Repo(".")
    commit = repo.head.commit
    config_path = os.path.join(out_dir, "config.yml")
    config=dict(
        n_samples=int(n_samples),
        dwi_path=dwi_path,
        trk_path=trk_path,
        model=model,
        block_size=int(block_size),
        commit=str(commit)
    )
    print("Saving {}".format(config_path))
    with open(config_path, "w") as file:
            yaml.dump(config, file, default_flow_style=False)
            
    return samples


if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description="Generate sample npz from DWI and TRK data.")

    parser.add_argument("dwi_path", help="Path to DWI file")

    parser.add_argument("trk_path", help="Path to TRK file")

    parser.add_argument("--model", default="conditional",
        choices=["conditional", "prior", "RNN"],
        help="Which model to generate samples for.")

    parser.add_argument("--block_size", help="Size of cubic neighborhood.",
        default=3, choices=[1,3,5,7], type=int)

    parser.add_argument("--n_samples", default=2**30, type=int,
        help="Maximum number of samples to keep.")

    parser.add_argument("--n_files", default=100, type=int,
        help="Number of output files of the conditional samples. "
             "For RNN samples it is determined dynamically")

    parser.add_argument("--out_dir", default=None, 
        help="Sample directory, by default creates directory next to dwi_path.")

    args = parser.parse_args()

    generate_samples(
        args.dwi_path,
        args.trk_path,
        args.model,
        args.block_size,
        args.n_samples,
        args.out_dir,
        args.n_files)
