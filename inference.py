import os
import gc
import argparse
import datetime
import yaml
import git

import nibabel as nib
import numpy as np

from tensorflow.keras.models import load_model
from tensorflow.keras import backend as K

from nibabel.streamlines.trk import TrkFile
from nibabel.streamlines.array_sequence import ArraySequence
from nibabel.streamlines.tractogram import Tractogram

from time import time

from models import MODELS

from utils.config import load
from utils.prediction import Prior, Terminator, get_blocksize
from utils.training import setup_env, maybe_get_a_gpu
from utils._score import score

from resample_trk import add_tangent

import configs


@setup_env
def run_inference(config=None, gpu_queue=None, return_to=None):

    """"""
    gpu_idx = -1
    try:
        gpu_idx = maybe_get_a_gpu() if gpu_queue is None else gpu_queue.get()
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_idx
    except Exception as e:
        print(str(e))

    print("Loading Models...") #################################################

    train_config_path = os.path.join(
        os.path.dirname(config['model_path']), "config.yml")

    model_name = load(train_config_path, "model_name")

    if hasattr(MODELS[model_name], "custom_objects"):
        model = load_model(config['model_path'],
                           custom_objects=MODELS[model_name].custom_objects,
                           compile=False)
    else:
        model = load_model(config['model_path'], compile=False)


    print("Loading DWI...") ####################################################

    dwi_img = nib.load(config['dwi_path'])
    dwi_img = nib.funcs.as_closest_canonical(dwi_img)
    dwi_aff = dwi_img.affine
    dwi_affi = np.linalg.inv(dwi_aff)
    dwi = dwi_img.get_data()

    def xyz2ijk(coords, snap=False):
        ijk = (coords.T).copy()
        dwi_affi.dot(ijk, out=ijk)
        if snap:
            return np.round(ijk, out=ijk).astype(int, copy=False).T
        else:
            return ijk.T

    ############################################################################

    terminator = Terminator(config['term_path'], config['thresh'])

    prior = Prior(config['prior_path'])

    print("Initializing Fibers...") ############################################

    seed_file = nib.streamlines.load(config['seed_path'])
    xyz = seed_file.tractogram.streamlines.data
    n_seeds = 2 * len(xyz)
    xyz = np.vstack([xyz, xyz])  # Duplicate seeds for both directions
    xyz = np.hstack([xyz, np.ones([n_seeds, 1])]) # add affine dimension
    xyz = xyz.reshape(-1, 1, 4)  # (fiber, segment, coord)

    fiber_idx = np.hstack([
        np.arange(n_seeds//2, dtype="int32"),
        np.arange(n_seeds//2,  dtype="int32")
    ])
    fibers = [[] for _ in range(n_seeds//2)]

    print("Start Iteration...") ################################################

    block_size = get_blocksize(model, dwi.shape[-1])

    for step in range(config['max_steps']):
        t0 = time()

        # Get coords of latest segement for each fiber
        ijk = xyz2ijk(xyz[:,-1,:], snap=True)
        n_ongoing = len(ijk)
        i,j,k, _ = ijk.T

        d = np.zeros([n_ongoing, block_size, block_size, block_size, dwi.shape[-1]])
        for idx in range(block_size**3):
            ii,jj,kk = np.unravel_index(idx, (block_size, block_size, block_size))
            d[:, ii, jj, kk, :] = dwi[i+ii-1, j+jj-1, k+kk-1, :]
        d = d.reshape(-1, dwi.shape[-1] * block_size**3)

        dnorm = np.linalg.norm(d, axis=1, keepdims=True) + 10**-2
        d /= dnorm

        if step == 0:
            inputs = np.hstack([prior(xyz[:, 0, :]), d, dnorm])
        else:
            inputs = np.hstack([vout, d, dnorm])

        chunk = 2**16  # 32768
        n_chunks = np.ceil(n_ongoing / chunk).astype(int)
        vout = np.zeros([n_ongoing, 3])
        for c in range(n_chunks):

            outputs = model(inputs[c * chunk : (c + 1) * chunk])

            if isinstance(outputs, list):
                outputs = outputs[0]

            if not 'predict_fn' in config:
                v = outputs
            elif config['predict_fn'] == "mean":
                v = outputs.mean_direction.numpy()
                # v = normalize(v)
            elif config['predict_fn'] == "sample":
                v = outputs.sample().numpy()
            vout[c * chunk : (c + 1) * chunk] = v

        rout = xyz[:, -1, :3] + config['step_size'] * vout
        rout = np.hstack([rout, np.ones((n_ongoing, 1))]).reshape(-1, 1, 4)

        xyz = np.concatenate([xyz, rout], axis=1)

        terminal_indices = terminator(xyz[:, -1, :])

        for idx in terminal_indices:
            gidx = fiber_idx[idx]
            # Other end not yet added
            if not fibers[gidx]:
                fibers[gidx].append(np.copy(xyz[idx, :, :3]))
            # Other end already added
            else:
                this_end = xyz[idx, :, :3]
                other_end = fibers[gidx][0]
                merged_fiber = np.vstack([
                    np.flip(this_end[1:], axis=0),
                    other_end]) # stitch ends together
                fibers[gidx] = [merged_fiber]

        xyz = np.delete(xyz, terminal_indices, axis=0)
        vout = np.delete(vout, terminal_indices, axis=0)
        fiber_idx = np.delete(fiber_idx, terminal_indices)

        print("Iter {:4d}/{}, finished {:5d}/{:5d} ({:3.0f}%) of all seeds with"
              " {:6.0f} steps/sec".format((step+1), config['max_steps'],
                                          n_seeds-n_ongoing, n_seeds,
                                          100*(1-n_ongoing/n_seeds),
                                          n_ongoing / (time() - t0)),
              end="\r")

        if n_ongoing == 0:
            break

        gc.collect()

    # Exclude unfinished fibers (finished = both ends finished)
    fibers = [fibers[gidx] for gidx in range(len(fibers)) if gidx not in fiber_idx]

    # Save Result

    fibers = [f[0] for f in fibers]

    tractogram = Tractogram(
        streamlines=ArraySequence(fibers),
        affine_to_rasmm=np.eye(4)
    )

    tractogram = add_tangent(
        tractogram,
        min_length=config["min_length"],
        max_length=config["max_length"]
    )

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
    out_dir = os.path.join(os.path.dirname(config["dwi_path"]),
        "predicted_fibers", timestamp)

    configs.deep_update(config, {"out_dir": out_dir})

    os.makedirs(out_dir, exist_ok=True)

    fiber_path = os.path.join(out_dir, timestamp + ".trk")
    print("\nSaving {}".format(fiber_path))
    TrkFile(tractogram, seed_file.header).save(fiber_path)

    config['training_config'] = load(train_config_path)
    repo = git.Repo(".")
    commit = repo.head.commit
    config['commit'] = str(commit)
    config_path = os.path.join(out_dir, "config.yml")
    print("Saving {}".format(config_path))
    with open(config_path, "w") as file:
        yaml.dump(config, file, default_flow_style=False)

    if config["score"]:
        score(
            fiber_path,
            out_dir=os.path.join(out_dir, "scorings"),
            no_trim=True,
            blocking=False,
            python2=config['python2'],
            )
        
    # Return GPU

    K.clear_session()
    if gpu_queue is not None:
        gpu_queue.put(gpu_idx)

    if return_to is not None:
        return_to[fiber_path] = {
        "model_path": config["model_path"],
        "dwi_path": config["dwi_path"]
        }

    return fiber_path


def infere_batch_seed(xyz, prior, terminator, model,
                      dwi, dwi_affi, max_steps, step_size, model_name=''):

    n_seeds = len(xyz)
    fiber_idx = np.hstack([
        np.arange(n_seeds//2, dtype="int32"),
        np.arange(n_seeds//2,  dtype="int32")
    ])
    fibers = [[] for _ in range(n_seeds//2)]

    def xyz2ijk(coords, snap=False):
        ijk = (coords.T).copy()
        dwi_affi.dot(ijk, out=ijk)
        if snap:
            return np.round(ijk, out=ijk).astype(int, copy=False).T
        else:
            return ijk.T

    block_size = get_blocksize(model, dwi.shape[-1])

    d = np.zeros([n_seeds, dwi.shape[-1] * block_size ** 3])
    dnorm = np.zeros([n_seeds, 1])
    vout = np.zeros([n_seeds, 3])
    already_terminated = np.empty(0, dtype="int32")
    mask = np.ones((n_seeds), dtype=bool)
    n_ongoing = n_seeds
    out_of_bound_fibers = 0
    for i in range(max_steps):
        t0 = time()

        # Get coords of latest segement for each fiber
        ijk = xyz2ijk(xyz[:, -1, :], snap=True)

        for ii, idx in enumerate(ijk):
            try:
                d[ii] = dwi[
                        idx[0]-(block_size // 2): idx[0]+(block_size // 2)+1,
                        idx[1]-(block_size // 2): idx[1]+(block_size // 2)+1,
                        idx[2]-(block_size // 2): idx[2]+(block_size // 2)+1,
                        :].flatten()  # returns copy
                dnorm[ii] = np.linalg.norm(d[ii]) + 0.01
                d[ii] /= dnorm[ii]
            except:
                assert ii in already_terminated
                out_of_bound_fibers = out_of_bound_fibers + 1

        if i == 0:
            inputs = np.hstack([prior(xyz[:, 0, :]), d, dnorm])
        else:
            inputs = np.hstack([vout, d, dnorm])

        if 'Entrack' in model_name:
            outputs = model(inputs[:, np.newaxis, :])
            if isinstance(outputs, list):
                outputs = outputs[0]

            if config['predict_fn'] == "mean":
                vout = outputs.mean_direction.numpy()
            elif config['predict_fn'] == "sample":
                vout = outputs.sample().numpy()
        else:
            vout = model.predict(inputs[:, np.newaxis, :]).squeeze()

        vout = np.squeeze(vout)
        rout = xyz[:, -1, :3] + step_size * vout
        rout = np.hstack([rout, np.ones((n_seeds, 1))]).reshape(-1, 1, 4)

        xyz = np.concatenate([xyz, rout], axis=1)

        mask[already_terminated] = False
        tmp_indices = terminator(xyz[mask, -1, :])
        terminal_indices = np.where(mask)[0][tmp_indices]

        for idx in terminal_indices:
            assert idx not in already_terminated
            gidx = fiber_idx[idx]
            # Other end not yet added
            if not fibers[gidx]:
                fibers[gidx].append(np.copy(xyz[idx, :, :3]))
            # Other end already added
            else:
                this_end = xyz[idx, :, :3]
                other_end = fibers[gidx][0]
                merged_fiber = np.vstack([
                    np.flip(this_end[1:], axis=0),
                    other_end])  # stitch ends together
                fibers[gidx] = [merged_fiber]

            n_ongoing = n_ongoing - 1
        already_terminated = np.concatenate(
            [already_terminated, terminal_indices])

        print("Iter {:4d}/{}, finished {:5d}/{:5d} ({:3.0f}%) of all seeds with"
              " {:6.0f} steps/sec".format((i + 1), max_steps,
                                          n_seeds - n_ongoing, n_seeds,
                                          100 * (1 - n_ongoing / n_seeds),
                                          n_ongoing / (time() - t0)),
              end="\r")

        if n_ongoing == 0:
            assert len(set(already_terminated)) == n_seeds
            print("normal termination")
            break

        gc.collect()

    print("{0} times fibers got out of bound, but keep calm as they were "
          "already finished".format(out_of_bound_fibers))

    # Exclude unfinished fibers:
    fibers = [fibers[gidx] for gidx in range(len(fibers)) if
              gidx in already_terminated]
    return fibers


def run_rnn_inference(config, gpu_queue=None):
    """"""

    gpu_idx = -1
    try:
        gpu_idx = maybe_get_a_gpu() if gpu_queue is None else gpu_queue.get()
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_idx
    except Exception as e:
        print(str(e))

    print("Loading DWI...")  ####################################################
    batch_size = config['batch_size']
    dwi_img = nib.load(config['dwi_path'])
    dwi_img = nib.funcs.as_closest_canonical(dwi_img)
    dwi_aff = dwi_img.affine
    dwi_affi = np.linalg.inv(dwi_aff)
    dwi = dwi_img.get_data()

    print("Loading Models...")  #################################################

    train_config_path = os.path.join(
        os.path.dirname(config['model_path']), "config.yml")

    with open(train_config_path, "r") as config_file:
        model_name = yaml.load(config_file)["model_name"]

    if hasattr(MODELS[model_name], "custom_objects"):
        trained_model = load_model(config['model_path'],
                           custom_objects=MODELS[model_name].custom_objects,
                           compile=False)
    else:
        trained_model = load_model(config['model_path'], compile=False)

    model_config = {'batch_size': batch_size,
                    'input_shape':  trained_model.input_shape[1:],
                    'temperature': 0.04}
    prediction_model = MODELS[model_name](model_config).keras
    prediction_model.set_weights(trained_model.get_weights())

    terminator = Terminator(config['term_path'], config['thresh'])

    prior = Prior(config['prior_path'])

    print("Initializing Fibers...")  ############################################

    seed_file = nib.streamlines.load(config['seed_path'])
    xyz = seed_file.tractogram.streamlines.data
    n_seeds = len(xyz)
    fibers = [[] for _ in range(n_seeds)]

    for i in range(0, n_seeds, batch_size // 2):
        xyz_batch = xyz[i:i + batch_size // 2]

        n_seeds_batch = 2 * len(xyz_batch)
        # Duplicate seeds for both directions
        xyz_batch = np.vstack([xyz_batch, xyz_batch])
        # add affine dimension
        xyz_batch = np.hstack([xyz_batch, np.ones([n_seeds_batch, 1])])
        # (fiber, segment, coord)
        xyz_batch = xyz_batch.reshape(-1, 1, 4)

        # Make a last model for the remaining batch
        if i == batch_size//2 * (n_seeds // (batch_size // 2)):
            last_batch_size = (n_seeds - i) * 2
            model_config['batch_size'] = last_batch_size
            prediction_model = MODELS[model_name](model_config).keras
            prediction_model.set_weights(trained_model.get_weights())

        prediction_model.reset_states()
        print("Batch {0} with shape {1}".format(
            i // (batch_size // 2), xyz_batch.shape))
        batch_fibers = infere_batch_seed(xyz_batch, prior, terminator,
            prediction_model, dwi, dwi_affi, config['max_steps'],
                                         config['step_size'], model_name=model_name)
        fibers[i:i+batch_size//2] = batch_fibers

    # Save Result
    fibers = [f[0] for f in fibers if len(f) > 0]

    tractogram = Tractogram(
        streamlines=ArraySequence(fibers),
        affine_to_rasmm=np.eye(4)
    )

    K.clear_session()
    if gpu_queue is not None:
        gpu_queue.put(gpu_idx)

    out_dir = config['out_dir']
    if out_dir is None:
        out_dir = os.path.dirname(config['dwi_path'])

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H:%M:%S")

    out_dir = os.path.join(out_dir, "predicted_fibers", timestamp)

    os.makedirs(out_dir, exist_ok=True)

    fiber_path = os.path.join(out_dir, "fibers.trk")
    print("\nSaving {}".format(fiber_path))
    TrkFile(tractogram, seed_file.header).save(fiber_path)

    repo = git.Repo(".")
    commit = repo.head.commit
    config_path = os.path.join(out_dir, "config.yml")
    config['training_config'] = load(train_config_path)
    config['commit'] = str(commit)
    print("Saving {}".format(config_path))
    with open(config_path, "w") as file:
        yaml.dump(config, file, default_flow_style=False)

    if config["score"]:
        score(
            fiber_path,
            out_dir=os.path.join(out_dir, "scorings"),
            min_length=config["min_length"],
            max_length=config["max_length"],
            python2=config['python2']
            )

    return tractogram


if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description="Use a trained model to predict fibers on DWI data.")

    parser.add_argument("config_path", type=str, nargs="?",
                        help="Path to inference config.")

    args, more_args = parser.parse_known_args()

    config = configs.compile_from(args.config_path, args, more_args)

    if config['model_name'].startswith("RNN"):
        run_rnn_inference(config)
    else:
        run_inference(config)


