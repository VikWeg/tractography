# Data-Driven Tractography

[inference_t.ipynb]: entrack/inference_t.ipynb
[training_conditional_t.ipynb]: entrack/training_conditional_t.ipynb 
[fiber_resampling.ipynb]: fiber_resampling.ipynb
[generate_conditional_samples_t.ipynb]: generate_conditional_samples_t.ipynb
[trk2seeds.ipynb]: trk2seeds.ipynb

[HCP project]: https://db.humanconnectome.org
[TractSeg]: https://zenodo.org/record/1477956#.XaN1YdszafZ

**Keywords**: Diffusion-Weighted MRI, Fiber Tracking, Machine Learning, Brain

## Purpose
This repository provides a utility to perform rapid prototyping of data-driven
tractography models.

## Overview
Using the notebooks in this repository assumes a certain file structure:

```
project/
├──models/
├──subjects/
|  └──992774/
|     ├──tracts/
|     ├──resampled_fibers/
|     ├──predicted_fibers/
|     ├──samples/
|     ├──seeds/
|     └──fod.nii.gz
├──entrack/
|  ├──inference_t.ipynb
|  └──training_conditional_t.ipynb
├──fiber_resampling.ipynb
├──generate_conditional_samples_t.ipynb
└──trk2seeds.ipynb
```

The file system is described shortly, for more details please refer to the
individual files.

### models/
Contains keras models trained in [training_conditional_t.ipynb], along with
yaml configs, which contain the training parameters.

### subjects/
This is the data folder comprising DWI data, training fibers, and their
processed forms.
Specifically, we use T1 and DWI data from the [HCP project], and reference
fibers from [TractSeg].

* **tracts/** Raw fiber bundles (.trk), downloaded from [TractSeg].
* **resampled_fibers/** Fibers with data (.trk), interpolated by [fiber_resampling.ipynb].
* **predicted_fibers/** Fibers (.trk), predicted by [inference_t.ipynb].
* **samples/** Collections of (vin, D, vout) samples (.npz), produced by [generate_conditional_samples_t.ipynb].
* **seeds/** Fiber seed files (.npy), generated by [generate_conditional_samples_t.ipynb].
* **fod.nii.gz** Additional DWI data, from [HCP project].

### entrack/
Folder for a specific model class, in this case Entrack. It defines how its
models are trained ([training_conditional_t.ipynb]), and how they are used to
predict fibers ([inference_t.ipynb]).

### [fiber_resampling.ipynb]
Notebook to resample fibers, and calculate local fiber geometry data, such as
tangent, curvature, and torsion.

### [generate_conditional_samples_t.ipynb]
Notebook which defines the generation of samples
$`(v_{in}, \mathbf{D}, v_{out})`$ for learning the conditional
distribution p((vin| D, vout).

### [trk2seeds.ipynb]
Small utility notebook to convert the fiber endpoints in a .trk file to seed 
coordinates (.npy).
