import os
import re
import gc
import yaml
import argparse
import numpy as np
import nibabel as nib

from multiprocessing import SimpleQueue, Process
from time import sleep

from scipy.sparse.csgraph import connected_components 

import tensorflow as tf
from tensorflow.keras import backend as K

from dipy.segment.clustering import QuickBundles
from dipy.segment.bundles import RecoBundles
from dipy.segment.metric import (AveragePointwiseEuclideanMetric,
    ResampleFeature, distance_matrix)
from dipy.tracking._utils import _mapping_to_voxel, _to_voxel_coordinates
from dipy.segment.clustering import Cluster, ClusterMap

from models.model_classes import FisherVonMises
from models import load_model

from resample_trk import maybe_add_tangent 
from utils.config import load
from utils.training import setup_env, maybe_get_a_gpu
from utils.prediction import get_blocksize
from utils._dispatch import get_gpus

from configs import save


@setup_env
def agreement(model_path, dwi_path_1, trk_path_1, dwi_path_2, trk_path_2,
    wm_path, fixel_cnt_path, cluster_thresh, centroid_size, fixel_thresh,
    bundle_min_cnt, gpu_queue=None):

    try:
        gpu_idx = maybe_get_a_gpu() if gpu_queue is None else gpu_queue.get()
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_idx
    except Exception as e:
        print(str(e))

    temperature = np.round(float(re.findall("T=(.*)\.h5", model_path)[0]), 6)
    model = load_model(model_path)

    print("Load data ...")

    dwi_img_1 = nib.load(dwi_path_1)
    dwi_img_1 = nib.funcs.as_closest_canonical(dwi_img_1)
    affine_1 = dwi_img_1.affine
    dwi_1 = dwi_img_1.get_data()

    dwi_img_2 = nib.load(dwi_path_2)
    dwi_img_2 = nib.funcs.as_closest_canonical(dwi_img_2)
    affine_2 = dwi_img_2.affine
    dwi_2 = dwi_img_2.get_data()

    wm_img = nib.load(wm_path)
    wm_data = wm_img.get_data()
    n_wm = (wm_data > 0).sum()

    fixel_cnt = nib.load(fixel_cnt_path).get_data()[:,:,:,0]
    fixel_cnt = fixel_cnt[wm_data>0]

    k_fixels = np.unique(fixel_cnt)
    max_fixels = k_fixels.max()
    n_fixels_gt = np.sum( k * (fixel_cnt == k).sum() for k in k_fixels)

    img_shape = dwi_1.shape[:-1]

    #---------------------------------------------------------------------------

    tractogram_1 = maybe_add_tangent(trk_path_1)
    tractogram_2 = maybe_add_tangent(trk_path_2)

    streamlines_1 = tractogram_1.streamlines
    streamlines_2 = tractogram_2.streamlines

    n_streamlines_1 = len(streamlines_1)
    n_streamlines_2 = len(streamlines_2)

    tractogram_1.extend(tractogram_2)

    ############################################################################

    print("Clustering streamlines.")

    feature = ResampleFeature(nb_points=centroid_size)

    qb = QuickBundles(
        threshold=cluster_thresh,
        metric=AveragePointwiseEuclideanMetric(feature)
    )

    bundles = qb.cluster(streamlines_1)
    bundles.refdata = tractogram_1

    n_bundles = len(bundles)

    print("Found {} bundles.".format(n_bundles))

    print("Computing bundle masks...")
    
    direction_masks_1 = np.zeros((n_bundles, ) + img_shape + (3, ), np.float16)
    direction_masks_2 = np.zeros((n_bundles, ) + img_shape + (3, ), np.float16)
    count_masks_1 = np.zeros((n_bundles, ) + img_shape, np.uint16)
    count_masks_2 = np.zeros((n_bundles, ) + img_shape, np.uint16)

    marginal_bundles = 0
    for i, b in enumerate(bundles.clusters):

        is_from_1 = np.argwhere(
            np.array(b.indices) < n_streamlines_1).squeeze().tolist()
        is_from_2 = np.argwhere(
            np.array(b.indices) >= n_streamlines_1).squeeze().tolist()

        if (np.sum(is_from_1) > bundle_min_cnt and
            np.sum(is_from_2) > bundle_min_cnt):

            counts_1, directions_1 = bundle_map(b[is_from_1], affine_1, img_shape)
            counts_2, directions_2 = bundle_map(b[is_from_2], affine_2, img_shape)

            direction_masks_1[i] = directions_1.copy("K")
            direction_masks_2[i] = directions_2.copy("K")

            count_masks_1[i] = counts_1.copy("K")
            count_masks_2[i] = counts_2.copy("K")
        else:
            marginal_bundles += 1

        print("Computed bundle {:3d}.".format(i), end="\r")

        gc.collect()

    overlap = (
        (count_masks_1 > 0) * (count_masks_2 > 0) * np.expand_dims(wm_data > 0, 0)
    )

    print("Calculating Fixels...")

    fixel_directions_1 = []
    fixel_directions_2 = []
    fixel_ijk = []
    n_fixels = []
    no_overlap = 0
    for vox in np.argwhere(wm_data > 0):

        matched = overlap[:, vox[0], vox[1], vox[2]] > 0

        if matched.sum() > 0:

            dir_1 = direction_masks_1[matched, vox[0], vox[1], vox[2], :]
            cnts_1 = count_masks_1[matched, vox[0], vox[1], vox[2]]

            dir_2 = direction_masks_2[matched, vox[0], vox[1], vox[2], :]
            cnts_2 = count_masks_2[matched, vox[0], vox[1], vox[2]]

            fixels1, fixels2 = cluster_fixels(
                dir_1, dir_2, cnts_1, cnts_2,
                threshold=np.cos(np.pi/fixel_thresh)
            )

            n_f = len(fixels1)

            fixel_directions_1.append(fixels1)
            fixel_directions_2.append(fixels2)
            fixel_ijk.append(np.tile(vox, (n_f, 1)))

            n_fixels.append(n_f)
        else:
            no_overlap += 1
        
    fixel_directions_1 = np.vstack(fixel_directions_1)
    fixel_directions_2 = np.vstack(fixel_directions_2)
    fixel_ijk = np.vstack(fixel_ijk)

    ############################################################################

    print("Computing agreement ...")

    n_fixels_sum = np.sum(n_fixels)

    block_size = get_blocksize(model, dwi_1.shape[-1])

    d_1 = np.zeros([
        n_fixels_sum,
        block_size,  block_size, block_size,
        dwi_1.shape[-1]
    ])
    d_2 = np.zeros([
        n_fixels_sum,
        block_size,  block_size, block_size,
        dwi_1.shape[-1]
    ])
    i,j,k = fixel_ijk.T
    for idx in range(block_size**3):
        ii,jj,kk = np.unravel_index(idx, (block_size, block_size, block_size))
        d_1[:, ii, jj, kk, :] = dwi_1[i+ii-1, j+jj-1, k+kk-1, :]
        d_2[:, ii, jj, kk, :] = dwi_2[i+ii-1, j+jj-1, k+kk-1, :]

    d_1 = d_1.reshape(-1, dwi_1.shape[-1] * block_size**3)
    d_2 = d_2.reshape(-1, dwi_2.shape[-1] * block_size**3)

    dnorm_1 = np.linalg.norm(d_1, axis=1, keepdims=True) + 10**-2
    dnorm_2 = np.linalg.norm(d_2, axis=1, keepdims=True) + 10**-2

    d_1 /= dnorm_1
    d_2 /= dnorm_2

    model_inputs_1 = np.hstack([fixel_directions_1, d_1, dnorm_1])
    model_inputs_2 = np.hstack([fixel_directions_2, d_2, dnorm_2])

    asum, amin, amean, amax = agreement_for(
        model,
        model_inputs_1,
        model_inputs_2
    )

    agreement = {"temperature": temperature}
    agreement["model_path"] = model_path
    agreement["n_bundles"] = n_bundles
    agreement["value"] = asum / n_fixels_gt
    agreement["min"] = amin
    agreement["mean"] = amean
    agreement["max"] = amax
    agreement["n_fixels_sum"] = n_fixels_sum
    agreement["n_wm"] = n_wm
    agreement["n_fixels_gt"] = n_fixels_gt
    agreement["marginal_bundles"] = marginal_bundles
    agreement["no_overlap"] = no_overlap
    agreement["dwi_1"] = dwi_path_1
    agreement["trk_1"] = trk_path_1
    agreement["dwi_2"] = dwi_path_2
    agreement["trk_2"] = trk_path_2
    agreement["fixel_cnt_path"] = fixel_cnt_path
    agreement["cluster_thresh"] = cluster_thresh
    agreement["centroid_size"] = centroid_size
    agreement["fixel_thresh"] = fixel_thresh
    agreement["bundle_min_cnt"] = bundle_min_cnt
    agreement["wm_path"] = wm_path
    agreement["ideal"] = ideal_agreement(temperature)
    for k, cnt in zip(*np.unique(n_fixels, return_counts=True)):
        agreement["n_vox_with_{}_fixels".format(k)] = cnt

    save(agreement,
        "agreement_T={}.yml".format(temperature),
        os.path.dirname(model_path)
    )

    K.clear_session()
    if gpu_queue is not None:
        gpu_queue.put(gpu_idx)


def logZ(kappa):
    expk2 = np.exp(- 2 * kappa)
    return np.log(2*np.pi) + kappa + np.log1p(- expk2) - np.log(kappa)


def ideal_agreement(T):
    return np.log(4*np.pi) + logZ(2/T) - 2*logZ(1/T)


def agreement_for(model, inputs1, inputs2):

    n_segments = len(inputs1)

    all_fvm_log_agreements = np.zeros(n_segments)

    chunk = 2**15  # 32768
    n_chunks = np.ceil(n_segments / chunk).astype(int)
    for c in range(n_chunks):

        fvm_pred_1, _ = model(
            inputs1[c * chunk : (c + 1) * chunk])

        fvm_pred_2, _ = model(
            inputs2[c * chunk : (c + 1) * chunk])

        all_fvm_log_agreements[c * chunk : (c + 1) * chunk] = (
            fvm_log_agreement(fvm_pred_1, fvm_pred_2)
        )

    all_fvm_log_agreements = np.maximum(0, all_fvm_log_agreements)

    return (
        all_fvm_log_agreements.sum(),
        all_fvm_log_agreements.min(),
        all_fvm_log_agreements.mean(),
        all_fvm_log_agreements.max()
    )


def bundle_map(bundle, affine, img_shape):

    lin_T, offset = _mapping_to_voxel(affine)
    counts = np.zeros(img_shape, np.uint16)
    directions = np.zeros(img_shape + (3,), np.float16)

    for tract in bundle:
        inds = _to_voxel_coordinates(tract.streamline, lin_T, offset)
        i, j, k = inds.T
        counts[i, j, k] += np.uint16(1)
        directions[i, j, k] += tract.data_for_points["t"].astype(np.float16)

    directions /= (np.expand_dims(counts, -1) + np.float16(10**-6))

    directions /= (
        np.linalg.norm(directions, axis=-1, keepdims=True) + np.float16(10**-6)
    )

    return counts, directions


def fvm_log_agreement(fvm1, fvm2):
    fvm12 = FisherVonMises(
        mean_direction=fvm1.mean_direction, # just a dummy, not used
        concentration=tf.norm(
            fvm1.mean_direction * fvm1.concentration[:, tf.newaxis] +
            fvm2.mean_direction * fvm2.concentration[:, tf.newaxis],
            axis=1)
    )
    return (
        np.log(4*np.pi) + 
        fvm12._log_normalization()
        - fvm1._log_normalization()
        - fvm2._log_normalization()
    )


def cluster_fixels(dir1, dir2, cnts1, cnts2, threshold):

    dotprod1 = dir1.dot(dir1.T)
    dotprod2 = dir2.dot(dir2.T)

    similarity = np.absolute(dotprod1) - np.eye(len(dir1), dtype=np.float16)

    imax, jmax = np.unravel_index(np.argmax(similarity), similarity.shape)

    if similarity[imax, jmax] > threshold:
        mean1 = (
            dir1[imax] * cnts1[imax]
            + np.sign(dotprod1[imax, jmax]) * dir1[jmax] * cnts1[jmax]
        )
        mean2 = (
            dir2[imax] * cnts2[imax]
            + np.sign(dotprod2[imax, jmax]) * dir2[jmax] * cnts2[jmax]
        )

        mean1 /= (np.linalg.norm(mean1) + np.float16(10**-6))
        mean2 /= (np.linalg.norm(mean2) + np.float16(10**-6))

        mean2 *= np.sign(mean1.dot(mean2.T))

        mcount1 = cnts1[imax] + cnts1[jmax]
        mcount2 = cnts2[imax] + cnts2[jmax]

        dir1 = np.delete(dir1, [imax, jmax], axis=0)
        cnts1 = np.delete(cnts1, [imax, jmax], axis=0)
        dir2 = np.delete(dir2, [imax, jmax], axis=0)
        cnts2 = np.delete(cnts2, [imax, jmax], axis=0)

        dir1 = np.vstack([dir1, mean1])
        cnts1 = np.vstack([cnts1.reshape(-1, 1), mcount1.reshape(-1, 1)])
        dir2 = np.vstack([dir2, mean2])
        cnts2 = np.vstack([cnts2.reshape(-1, 1), mcount2.reshape(-1, 1)])

        return cluster_fixels(dir1, dir2, cnts1, cnts2, threshold)
    else:
        return dir1, dir2



if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description="Calculate agreement.")

    parser.add_argument("config_path", type=str, nargs="?")

    parser.add_argument("--wm_path", help="Path to .nii file", type=str)

    parser.add_argument("--fixel_cnt_path", help="Path to .nii file", type=str)

    parser.add_argument("--dwi_path_1", help="Path to .nii file", type=str)

    parser.add_argument("--dwi_path_2", help="Path to .nii file", type=str)

    parser.add_argument("--trk_path_1", help="Path to .trk file", type=str)

    parser.add_argument("--trk_path_2", help="Path to .trk file", type=str)

    parser.add_argument("--model_path", help="Path to .h5 file", type=str)

    parser.add_argument("--cthresh", help="Bundle clustering threshold",
        type=float, default=20., dest="cluster_thresh")

    parser.add_argument("--centroid_size", help="Length of fiber centroids",
        type=int, default=200)

    parser.add_argument("--bmin", help="Minimal bundle size",
        type=int, default=10, dest="bundle_min_cnt")

    parser.add_argument("--fthresh", help="Fixel threshold (as fraction of pi)",
        type=float, default=6., dest="fixel_thresh")

    args = parser.parse_args()

    if args.config_path is not None:

        config = load(args.config_path)

        gpu_queue = SimpleQueue()
        for idx in get_gpus()[:5]:
            gpu_queue.put(str(idx))

        try:
            procs=[]
            for model_path, pair in config["pred_pairs"].items():
                if any(t in model_path for t in [
                    '0.0245', '0.0157', '0.0103', '0.0196', '0.0128',
                    '0.0018', '0.0067', '0.0010', '0.0300', '0.0084']):

                    while gpu_queue.empty():
                        sleep(10)

                    sleep(10)
                    p = Process(
                        target=agreement,
                        args=(model_path,
                              pair[0]["dwi_path"],
                              pair[0]["trk_path"],
                              pair[1]["dwi_path"],
                              pair[1]["trk_path"],
                              config["wm_path"],
                              config["fixel_cnt_path"],
                              config["cluster_thresh"],
                              config["centroid_size"],
                              config["fixel_thresh"],
                              config["bundle_min_cnt"],
                              gpu_queue)
                    )
                    procs.append(p)
                    p.start()

        except KeyboardInterrupt:
            pass
        finally:
            for p in procs:
                p.join()
                while p.exitcode is None:
                    sleep(0.1)
    else:
        agreement(
            args.model_path,
            args.dwi_path_1,
            args.trk_path_1,
            args.dwi_path_2,
            args.trk_path_2,
            args.wm_path,
            args.fixel_cnt_path,
            args.cluster_thresh,
            args.fixel_thresh,
            args.bundle_min_cnt,
            args.centroid_size
        )