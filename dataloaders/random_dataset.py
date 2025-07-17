import os
from os.path import join
import cv2
from einops import rearrange
import torch
import numpy as np
import tempfile, shutil
import glob
import logging
from torch.utils.data import Dataset
from torchvision.transforms import Compose, Resize
from PIL import Image
import torch.distributed as dist
from tqdm import tqdm
from .depthanything_preprocess import _load_and_process_image, _load_and_process_depth

class RandomDataset(Dataset):
    def __init__(self, root_dir, resolution=None, crop_type=None, large_dir=None):
        self.root_dir = root_dir
        self.resolution = resolution
        self.crop_type = crop_type
        self.large_dir = large_dir

        if self.root_dir.endswith('.mp4') or self.root_dir.endswith('.avi'):
            self.seq_paths = [self.root_dir]
        elif os.path.isdir(self.root_dir):
            self.seq_paths = glob.glob(join(self.root_dir, '*.mp4')) + glob.glob(join(self.root_dir, '*.avi'))
            self.seq_paths = sorted(self.seq_paths)
        else:
            raise ValueError(f"provide an mp4/avi file or a directory of mp4/avi files")

        
        
    def __len__(self):
        return len(self.seq_paths)
        
    def __getitem__(self, idx):
        limited_cpu_memory = True



        img_paths, tmpdirname, video_fps = self.parse_seq_path(self.seq_paths[idx])
        img_paths = sorted(img_paths, key=lambda x: int(''.join(filter(str.isdigit, os.path.basename(x)))))
        imgs = []

        first_img = cv2.imread(img_paths[0])
        h, w = first_img.shape[:2]
        if max(h, w) > 2044: # set max long side to 2044
            logging.info("resizing long side of video to 2044")
            scale = 2044 / max(h, w)
            res = (int(w * scale), int(h * scale))
            logging.info(f"new resolution: {res}")
        else:
            res = (w, h)

        

        # for img_path in img_paths:
        imgs_length = 0
        npy_paths = []
        for img_path in tqdm(img_paths, desc=f"PreProcessing  {os.path.basename(self.seq_paths[idx])}"):
            img, _ = _load_and_process_image(img_path, resolution=res, crop_type=None) 
            print(f"after processing img shape: {img.shape}, dtype: {img.dtype}")
            # print( f"max img value: {img.max()}, min img value: {img.min()}")

            
            if limited_cpu_memory:
                # save img
                img = img.cpu() # float64
                # print(f"img shape: {img.shape}, dtype: {img.dtype}")
                img = rearrange(img, 'c h w -> h w c')
                # save as it is as npy
                npy_path = os.path.join(tmpdirname, f"frame_{imgs_length}.npy")
                npy_paths.append(npy_path)
                np.save(npy_path, img.numpy())
                imgs_length += 1
            else:
                imgs.append(img)


        if tmpdirname is not None and not limited_cpu_memory:
        # after the loop, if we have enough CPU memory, we can rm the tmpdirname
            logging.info(f"Removing temporary directory {tmpdirname}")
            shutil.rmtree(tmpdirname)

        return dict(batch=torch.stack(imgs).float() if not limited_cpu_memory else torch.empty(0),
                scene_name=os.path.basename(self.seq_paths[idx].split('.')[0]), img_paths=npy_paths, fps=video_fps)

    def parse_seq_path(self, p):
        cap = cv2.VideoCapture(p)
        if not cap.isOpened():
            raise ValueError(f"Error opening video file {p}")
        video_fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # for debugging purposes
        total_frames = min(total_frames, 2) 
        if video_fps == 0:
            cap.release()
            raise ValueError(f"Error: Video FPS is 0 for {p}")
        frame_interval = 1
        frame_indices = list(range(0, total_frames, frame_interval))
        print(
            f" - Video FPS: {video_fps}, Frame Interval: {frame_interval}, Total Frames to Read: {len(frame_indices)}"
        )
        img_paths = []
        tmpdirname = tempfile.mkdtemp()
        # for i in frame_indices:
        for i in tqdm(frame_indices, desc=f"Parsing frames {os.path.basename(p)}"):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret:
                break
            frame_path = os.path.join(tmpdirname, f"frame_{i}.jpg")
            print(f"frame size: {frame.shape}, saving to {frame_path}")
            cv2.imwrite(frame_path, frame)
            img_paths.append(frame_path)
        cap.release()
        return img_paths, tmpdirname, video_fps