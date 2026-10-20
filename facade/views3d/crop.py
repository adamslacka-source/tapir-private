import sys, glob, os
from PIL import Image
for f in glob.glob(os.path.join(sys.argv[1], 'bim3d_*.png')):
    if f.endswith('_crop.png'): continue
    im = Image.open(f); w, h = im.size; cw = int(h * 1.6)
    im.crop(((w - cw)//2, 0, (w + cw)//2, h)).save(f[:-4] + '_crop.png'); print(f)
