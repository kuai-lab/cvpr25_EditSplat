#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

from argparse import ArgumentParser, Namespace
import sys
import os
import json

class GroupParams:
    pass

class ParamGroup:
    def __init__(self, parser: ArgumentParser, name : str, fill_none = False):
        group = parser.add_argument_group(name)
        for key, value in vars(self).items():
            shorthand = False
            if key.startswith("_"):
                shorthand = True
                key = key[1:]
            t = type(value)
            value = value if not fill_none else None 
            if shorthand:
                if t == bool:
                    group.add_argument("--" + key, ("-" + key[0:1]), default=value, action="store_true")
                else:
                    group.add_argument("--" + key, ("-" + key[0:1]), default=value, type=t)
            else:
                if t == bool:
                    group.add_argument("--" + key, default=value, action="store_true")
                else:
                    group.add_argument("--" + key, default=value, type=t)

    def extract(self, args):
        group = GroupParams()
        for arg in vars(args).items():
            if arg[0] in vars(self) or ("_" + arg[0]) in vars(self):
                setattr(group, arg[0], arg[1])
        return group

class ModelParams(ParamGroup): 
    def __init__(self, parser, sentinel=False):
        self.sh_degree = 3
        self._source_path = ""
        self._model_path = ""
        self.source_checkpoint = ""
        self._images = "images"
        self._resolution = -1
        self._white_background = False
        self.data_device = "cuda"
        self.eval = True
        self.render_items = ['RGB', 'Depth', 'Edge', 'Normal', 'Curvature', 'Feature Map']
        self.view_shuffling = False

        super().__init__(parser, "Loading Parameters", sentinel)

    def extract(self, args):
        g = super().extract(args)
        g.source_path = os.path.abspath(g.source_path)
        return g

class PipelineParams(ParamGroup):
    def __init__(self, parser):
        self.convert_SHs_python = False
        self.compute_cov3D_python = False
        self.debug = True
        super().__init__(parser, "Pipeline Parameters")

class EditingParams(ParamGroup):
    def __init__(self, parser):
        self.target_prompt = ""
        self.sampling_prompt = ""
        self.object_prompt = ""
        self.target_mask_prompt = ""
        self.text_guidance_scale = 7.5
        self.image_guidance_scale = 1.5
        self.MFG_scale = 1.0
        self.source_guidance_scale = 0.5
        self.filtering_ratio = 0.85

        super().__init__(parser, "Editing Parameters")

class OptimizationParams(ParamGroup):
    def __init__(self, parser):
        self.iterations = 30_000
        self.epoch = 10
        self.position_lr_init = 0.00016 
        self.position_lr_final = 0.0000016 
        self.position_lr_delay_mult = 0.01 
        self.position_lr_max_steps = 30_000
        self.feature_lr = 0.0025
        self.opacity_lr = 0.025
        self.scaling_lr = 0.005 
        self.rotation_lr = 0.001 

        self.percent_dense = 0.01
        self.lambda_dssim = 0.2
        self.densification_interval = 100
        self.opacity_reset_interval = 3000 
        self.densify_from_iter = 500
        self.densify_until_iter = 15_000 
        self.densify_grad_threshold = 0.01
        self.ply_path = ""
        
        self.attn_thres = 0.1
        self.k_percent = 0.15
        
        super().__init__(parser, "Optimization Parameters")


def get_combined_args(parser: ArgumentParser):
    cmdlne_string = sys.argv[1:]
    args_cmdline = parser.parse_args(cmdlne_string)
    cfgfile_data = {}
    cfgfilepath = os.path.join(args_cmdline.model_path, "args.json")
    try:
        print("Looking for config file in", cfgfilepath)
        with open(cfgfilepath, "r") as cfg_file:
            print("Config file found:", cfgfilepath)
            cfgfile_data = json.load(cfg_file)
    except (FileNotFoundError, json.JSONDecodeError):
        print("Config file not found or invalid JSON at", cfgfilepath)

    merged_dict = cfgfile_data.copy()
    for k, v in vars(args_cmdline).items():
        if v is not None:
            merged_dict[k] = v

    return Namespace(**merged_dict)