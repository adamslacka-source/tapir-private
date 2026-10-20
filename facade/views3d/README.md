# 3D views of the BIM facades

`python -I shots3d.py <this folder> <out folder> "<spec.json>=<tag>,..."` – hides every visible layer that is not
`FASADA*`, places a two-point perspective camera in front of each facade (reference a-b, outside side, z0 from the
spec), rebuilds the 3D view, saves `bim3d_<tag>_{celkovy,sikmy}.png`, and in `finally` restores the layers and the
original camera. `crop.py <out folder>` crops the very wide 3D window around the centre.
The 3D window must be allowed to rebuild (`RebuildView`) after `Set3DProjectionSettings`, otherwise the old view is saved.
