import json
import os
from pathlib import Path

import hydra
import matplotlib.pyplot as plt
import numpy as np
import torch
from monai.data import MetaTensor
from omegaconf import DictConfig, ListConfig

from echotk.sector_tools.ransac_sector_validation import ransac_sector_w_metrics
from echotk.utils.ascent_predictor import CustomASCENTPredictor
from echotk.utils.file_utils import open_nifti_file, save_nifti_file
from echotk.utils.viz_utils import show_gif


def extract_sector(cfg: DictConfig):
    out_path = Path(cfg.output)
    out_path.mkdir(exist_ok=True, parents=True)

    nnunet = CustomASCENTPredictor(model_name='sector_3d', use_tta=cfg.use_tta)

    if isinstance(cfg.input, ListConfig):
        pred = nnunet.predict_from_paths(cfg.input, cfg.nnunet_ckpt)
        filenames = cfg.input
        vol = [open_nifti_file(p) for p in cfg.input]
    elif Path(cfg.input).is_dir():
        filenames = [p for p in Path(cfg.input).glob("*.nii.gz")]
        pred = nnunet.predict_from_paths(filenames, cfg.nnunet_ckpt)
        vol = [open_nifti_file(p) for p in filenames]
    elif Path(cfg.input).is_file():
        vol, hdr, aff = open_nifti_file(cfg.input)
        filename = Path(cfg.input)
        # create initial mask using nnUnet
        data = np.expand_dims(vol.copy(), 0)
        meta = {"filename_or_obj": filename, "pixdim": hdr['pixdim']}
        pred = nnunet.predict_from_numpy(
                [{'image': MetaTensor(torch.tensor(data, dtype=torch.float32), meta=meta)}], cfg.nnunet_ckpt)
        filenames = [filename]
        vol = [(vol, hdr, aff)]
    else:
        raise Exception("Invalid input file format")

    # zip with filenames
    for p, v, f in zip(pred, vol, filenames):
        # compute final mask with ransac and return metrics used for validity
        ransac_mask, diff, ratio, annot, sig, ransac_param_dict = ransac_sector_w_metrics(p.astype(np.uint8),
                                                                                          img=v[0].copy(),
                                                                                          plot=cfg.show_intermediate_plots)

        # Check if ransac mask passes metrics
        # combining metrics with diff means that we trust the nnUnet segmentation to be very good
        # It is possible that saturated images or other differences may trigger invalid results,
        # use these metrics accordingly
        passed = True
        if diff > cfg.ransac_thresh.diff and sig > cfg.ransac_thresh.signal_lost:
            if cfg.verbose:
                print(f"Difference between masks {diff}, signal lost {sig}")
            passed = False
        if ratio < cfg.ransac_thresh.ratio:
            if cfg.verbose:
                print(f"Mask ratio is too small {ratio}")
            passed = False
        if diff > cfg.ransac_thresh.diff and annot > cfg.ransac_thresh.remaining_annotations:
            if cfg.verbose:
                print(f"Annotations remain {annot}")
            passed = False

        # log metrics to dataframe
        metrics = {
            'ransac_params': ransac_param_dict,
            'valid': passed,
            'diff': diff,
            'signal_lost': sig,
            'mask_cov_ratio': ratio,
            'annotations_remain': annot,
        }

        # appy mask and normalisation
        masked_image = v[0].copy()
        masked_image[~ransac_mask] = 0

        f_name = Path(f).stem.split('.')[0]

        if cfg.show_result_gifs:
            p_gif = show_gif(p.transpose((2, 1, 0)), f'{f_name}: Initial nn-UNet prediction')
            im_gif = show_gif(v[0].transpose((2, 1, 0)), f'{f_name}: Original input image')
            m_gif = show_gif(masked_image.transpose((2, 1, 0)), f'{f_name}: Masked output image')
            plt.show()

        # save mask and metrics dict (if wanted)
        save_nifti_file(f"{out_path}/{f_name}.nii.gz", masked_image, v[1], v[2])
        if cfg.save_metrics:
            with open(f"{out_path}/{f_name}_metrics.json", "w") as outfile:
                json.dump(metrics, outfile)


@hydra.main(version_base="1.2", config_path="config", config_name="sector_extract.yaml")
def main(cfg: DictConfig):
    # set project root
    os.environ['PROJECT_ROOT'] = os.path.join("/", *os.path.abspath(__file__).split('/')[:-2])
    # run
    extract_sector(cfg)


if __name__ == '__main__':
    main()
