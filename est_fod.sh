#!/bin/bash

# Usage: ./est_fod subjectID
# Example: ./est_fod 917255

dir="subjects/$1"

dwi2response \
tournier \
"${dir}/data_input.nii.gz" \
"${dir}/response.txt" \
-fslgrad "${dir}/bvecs_input" "${dir}/bvals_input" \
-mask "${dir}/dwi_brain_mask.nii.gz" &&

dwi2fod \
csd \
"${dir}/data_input.nii.gz" \
"${dir}/response.txt" \
"${dir}/fod.nii.gz" \
-lmax 4 \
-fslgrad "${dir}/bvecs_input" "${dir}/bvals_input" &&

mtnormalise \
"${dir}/fod.nii.gz" \
"${dir}/fod_norm.nii.gz" \
-mask "${dir}/dwi_brain_mask.nii.gz" &&

if [[ $dir =~ "ismrm" ]]
then
    mrresize \
    "${dir}/fod_norm.nii.gz" \
    "${dir}/fod_norm_125.nii.gz" \
    -voxel 1.25
fi
