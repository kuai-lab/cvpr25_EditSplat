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

import torch
import numpy as np
from utils.general_utils import (
    inverse_sigmoid,
    get_expon_lr_func,
    build_rotation,
)
from torch import nn
import os
from utils.system_utils import mkdir_p
from plyfile import PlyData, PlyElement
from utils.sh_utils import RGB2SH
from simple_knn._C import distCUDA2
from utils.graphics_utils import BasicPointCloud
from utils.general_utils import (
    strip_symmetric,
    build_scaling_rotation,
)
# from gaussian_renderer import camera2rasterizer

import math
from diff_gaussian_rasterization import (
    GaussianRasterizationSettings,
    GaussianRasterizer,
)

# from knn import K_nearest_neighbors
def camera2rasterizer(viewpoint_camera, bg_color: torch.Tensor, sh_degree: int = 0):
    tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
    tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)

    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=bg_color,
        scale_modifier=1.0,
        viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform,
        sh_degree=sh_degree,
        campos=viewpoint_camera.camera_center,
        prefiltered=False,
        debug=False,
    )

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)

    return rasterizer


class GaussianModel:
    def setup_functions(self):
        def build_covariance_from_scaling_rotation(scaling, scaling_modifier, rotation):
            L = build_scaling_rotation(scaling_modifier * scaling, rotation)
            actual_covariance = L @ L.transpose(1, 2)
            symm = strip_symmetric(actual_covariance)
            return symm

        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = build_covariance_from_scaling_rotation

        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid

        self.rotation_activation = torch.nn.functional.normalize

    def __init__(
            self,
            sh_degree: int,
    ):
        self.active_sh_degree = 0
        self.max_sh_degree = sh_degree
        self._xyz = torch.empty(0)
        self._features_dc = torch.empty(0)
        self._features_rest = torch.empty(0)
        self._scaling = torch.empty(0)
        self._rotation = torch.empty(0)
        self._opacity = torch.empty(0)
        self.max_radii2D = torch.empty(0)
        self.xyz_gradient_accum = torch.empty(0)
        self.denom = torch.empty(0)
        self.optimizer = None
        self.percent_dense = 0
        self.spatial_lr_scale = 0
        self.setup_functions()
        self.localize = False

        # self.attn_mask = torch.empty(0)
        # self.densi_attn_mask = torch.empty(0)

        self.densi_mask = torch.empty(0)

    def capture(self):
        return (
            self.active_sh_degree,
            self._xyz,
            self._features_dc,
            self._features_rest,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            self.spatial_lr_scale,
        )

    def restore(self, model_args, training_args):
        (
            self.active_sh_degree,
            self._xyz,
            self._features_dc,
            self._features_rest,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            xyz_gradient_accum,
            denom,
            opt_dict,
            self.spatial_lr_scale,
        ) = model_args
        self.training_setup(training_args)
        # self.load_ply(training_args.ply_path)
        self.xyz_gradient_accum = xyz_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)

    def prune_with_mask(self, new_mask=None):
        self.prune_points(self.mask)  # all the mask with value 1 are pruned
        if new_mask is not None:
            self.mask = new_mask
        else:
            self.mask[:] = 1  # all updatable
        self.remove_grad_mask()
        self.apply_grad_mask(self.mask)


    @property
    def get_scaling(self):
        if self.localize:
            return self.scaling_activation(self._scaling[self.mask])
        else:
            return self.scaling_activation(self._scaling)

    @property
    def get_rotation(self):
        if self.localize:
            return self.rotation_activation(self._rotation[self.mask])
        else:
            return self.rotation_activation(self._rotation)

    @property
    def get_xyz(self):
        if self.localize:
            return self._xyz[self.mask]
        else:
            return self._xyz

    @property
    def get_features(self):
        if self.localize:
            features_dc = self._features_dc[self.mask]
            features_rest = self._features_rest[self.mask]
        else:
            features_dc = self._features_dc
            features_rest = self._features_rest

        return torch.cat((features_dc, features_rest), dim=1)

    @property
    def get_opacity(self):
        if self.localize:
            return self.opacity_activation(self._opacity[self.mask])
        else:
            return self.opacity_activation(self._opacity)

    def get_covariance(self, scaling_modifier=1):
        if self.localize:
            return self.covariance_activation(
                self.get_scaling[self.mask], scaling_modifier, self._rotation[self.mask]
            )
        else:
            return self.covariance_activation(
                self.get_scaling, scaling_modifier, self._rotation
            )

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def create_from_pcd(self, pcd: BasicPointCloud, spatial_lr_scale: float):
        self.spatial_lr_scale = spatial_lr_scale
        fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
        fused_color = RGB2SH(torch.tensor(np.asarray(pcd.colors)).float().cuda())
        features = (
            torch.zeros((fused_color.shape[0], 3, (self.max_sh_degree + 1) ** 2))
            .float()
            .cuda()
        )
        features[:, :3, 0] = fused_color
        features[:, 3:, 1:] = 0.0

        print("Number of points at initialisation : ", fused_point_cloud.shape[0])

        dist2 = torch.clamp_min(
            distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()),
            0.0000001,
        )
        scales = torch.log(torch.sqrt(dist2))[..., None].repeat(1, 3)
        rots = torch.zeros((fused_point_cloud.shape[0], 4), device="cuda")
        rots[:, 0] = 1

        opacities = 1.0 * torch.ones(
            (fused_point_cloud.shape[0], 1), dtype=torch.float, device="cuda"
        )

        self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
        self._features_dc = nn.Parameter(
            features[:, :, 0:1].transpose(1, 2).contiguous().requires_grad_(True)
        )
        self._features_rest = nn.Parameter(
            features[:, :, 1:].transpose(1, 2).contiguous().requires_grad_(True)
        )
        self._scaling = nn.Parameter(scales.requires_grad_(True))
        self._rotation = nn.Parameter(rots.requires_grad_(True))
        self._opacity = nn.Parameter(opacities.requires_grad_(True))
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")
        self.active_sh_degree
        
        self.set_mask(
            torch.ones(
                self._opacity.shape[0],
                dtype=torch.bool,
                device="cuda",
                requires_grad=False,
            )
        )
        self.apply_grad_mask(self.mask)

    def training_setup(self, training_args):
        self.percent_dense = training_args.percent_dense
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")

        l = [
            {
                "params": [self._xyz],
                "lr": training_args.position_lr_init * self.spatial_lr_scale,
                "name": "xyz",
            },
            {
                "params": [self._features_dc],
                "lr": training_args.feature_lr,
                "name": "f_dc",
            },
            {
                "params": [self._features_rest],
                "lr": training_args.feature_lr / 20.0,
                "name": "f_rest",
            },
            {
                "params": [self._opacity],
                "lr": training_args.opacity_lr,
                "name": "opacity",
            },
            {
                "params": [self._scaling],
                "lr": training_args.scaling_lr,
                "name": "scaling",
            },
            {
                "params": [self._rotation],
                "lr": training_args.rotation_lr,
                "name": "rotation",
            },
        ]
        self.params_list = l
        self.optimizer = torch.optim.Adam(l, lr=0.0, eps=1e-15)
        self.xyz_scheduler_args = get_expon_lr_func(
            lr_init=training_args.position_lr_init * self.spatial_lr_scale,
            lr_final=training_args.position_lr_final * self.spatial_lr_scale,
            lr_delay_mult=training_args.position_lr_delay_mult,
            max_steps=training_args.position_lr_max_steps,
        )



    def update_learning_rate(self, iteration):
        """Learning rate scheduling per step"""
        for param_group in self.optimizer.param_groups:
            if param_group["name"] == "xyz":
                lr = self.xyz_scheduler_args(iteration)
                param_group["lr"] = lr

    def construct_list_of_attributes(self):
        l = ["x", "y", "z", "nx", "ny", "nz"]
        # All channels except the 3 DC
        for i in range(self._features_dc.shape[1] * self._features_dc.shape[2]):
            l.append("f_dc_{}".format(i))
        for i in range(self._features_rest.shape[1] * self._features_rest.shape[2]):
            l.append("f_rest_{}".format(i))
        l.append("opacity")
        for i in range(self._scaling.shape[1]):
            l.append("scale_{}".format(i))
        for i in range(self._rotation.shape[1]):
            l.append("rot_{}".format(i))
        return l

    def save_ply(self, path):
        # import pdb; pdb.set_trace()
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        f_dc = (
            self._features_dc.detach()
            .transpose(1, 2)
            .flatten(start_dim=1)
            .contiguous()
            .cpu()
            .numpy()
        )
        f_rest = (
            self._features_rest.detach()
            .transpose(1, 2)
            .flatten(start_dim=1)
            .contiguous()
            .cpu()
            .numpy()
        )
        opacities = self._opacity.detach().cpu().numpy()
        scale = self._scaling.detach().cpu().numpy()
        rotation = self._rotation.detach().cpu().numpy()

        dtype_full = [
            (attribute, "f4") for attribute in self.construct_list_of_attributes()
        ]

        elements = np.empty(xyz.shape[0], dtype=dtype_full)

        attributes = np.concatenate(
            (xyz, normals, f_dc, f_rest, opacities, scale, rotation), axis=1
        )
   
        for i in range(len(elements)):
            elements[i] = tuple(attributes[i])

        el = PlyElement.describe(elements, "vertex")
        PlyData([el]).write(path)

    def reset_opacity(self):
        opacities_new = inverse_sigmoid(
            torch.min(self.get_opacity, torch.ones_like(self.get_opacity) * 0.01)
        )
        optimizable_tensors = self.replace_tensor_to_optimizer(opacities_new, "opacity")
        self._opacity = optimizable_tensors["opacity"]

    # load ply
    def load_ply(self, path):
        plydata = PlyData.read(path)

        xyz = np.stack(
            (
                np.asarray(plydata.elements[0]["x"]),
                np.asarray(plydata.elements[0]["y"]),
                np.asarray(plydata.elements[0]["z"]),
            ),
            axis=1,
        )
        opacities = np.asarray(plydata.elements[0]["opacity"])[..., np.newaxis]

        features_dc = np.zeros((xyz.shape[0], 3, 1))
        features_dc[:, 0, 0] = np.asarray(plydata.elements[0]["f_dc_0"])
        features_dc[:, 1, 0] = np.asarray(plydata.elements[0]["f_dc_1"])
        features_dc[:, 2, 0] = np.asarray(plydata.elements[0]["f_dc_2"])

        extra_f_names = [
            p.name
            for p in plydata.elements[0].properties
            if p.name.startswith("f_rest_")
        ]
        extra_f_names = sorted(extra_f_names, key=lambda x: int(x.split("_")[-1]))
        # assert len(extra_f_names)==3*(self.max_sh_degree + 1) ** 2 - 3
        self.max_sh_degree = int(((len(extra_f_names) + 3) / 3) ** 0.5 - 1)
        features_extra = np.zeros((xyz.shape[0], len(extra_f_names)))
        for idx, attr_name in enumerate(extra_f_names):
            features_extra[:, idx] = np.asarray(plydata.elements[0][attr_name])
        # Reshape (P,F*SH_coeffs) to (P, F, SH_coeffs except DC)
        features_extra = features_extra.reshape(
            (features_extra.shape[0], 3, (self.max_sh_degree + 1) ** 2 - 1)
        )

        scale_names = [
            p.name
            for p in plydata.elements[0].properties
            if p.name.startswith("scale_")
        ]
        scale_names = sorted(scale_names, key=lambda x: int(x.split("_")[-1]))
        scales = np.zeros((xyz.shape[0], len(scale_names)))
        for idx, attr_name in enumerate(scale_names):
            scales[:, idx] = np.asarray(plydata.elements[0][attr_name])

        rot_names = [
            p.name for p in plydata.elements[0].properties if p.name.startswith("rot")
        ]
        rot_names = sorted(rot_names, key=lambda x: int(x.split("_")[-1]))
        rots = np.zeros((xyz.shape[0], len(rot_names)))
        for idx, attr_name in enumerate(rot_names):
            rots[:, idx] = np.asarray(plydata.elements[0][attr_name])

        self._xyz = nn.Parameter(
            torch.tensor(xyz, dtype=torch.float, device="cuda").requires_grad_(True)
        )
        self._features_dc = nn.Parameter(
            torch.tensor(features_dc, dtype=torch.float, device="cuda")
            .transpose(1, 2)
            .contiguous()
            .requires_grad_(True)
        )
        self._features_rest = nn.Parameter(
            torch.tensor(features_extra, dtype=torch.float, device="cuda")
            .transpose(1, 2)
            .contiguous()
            .requires_grad_(True)
        )
        self._opacity = nn.Parameter(
            torch.tensor(opacities, dtype=torch.float, device="cuda").requires_grad_(
                True
            )
        )
        self._scaling = nn.Parameter(
            torch.tensor(scales, dtype=torch.float, device="cuda").requires_grad_(True)
        )
        self._rotation = nn.Parameter(
            torch.tensor(rots, dtype=torch.float, device="cuda").requires_grad_(True)
        )

        self.active_sh_degree = self.max_sh_degree
        self.set_mask(
            torch.ones(
                self._opacity.shape[0],
                dtype=torch.bool,
                device="cuda",
                requires_grad=False,
            )
        )
        self.apply_grad_mask(self.mask)

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group["params"][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group["params"][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group["params"][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            stored_state = self.optimizer.state.get(group["params"][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]

                del self.optimizer.state[group["params"][0]]
                group["params"][0] = nn.Parameter(
                    (group["params"][0][mask].requires_grad_(True))
                )
                self.optimizer.state[group["params"][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(
                    group["params"][0][mask].requires_grad_(True)
                )
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def prune_points(self, mask):
        valid_points_mask = ~mask
        optimizable_tensors = self._prune_optimizer(valid_points_mask)

        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]

        self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]

        self.denom = self.denom[valid_points_mask]
        self.max_radii2D = self.max_radii2D[valid_points_mask]

        self.mask = self.mask[valid_points_mask]

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            assert len(group["params"]) == 1
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group["params"][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = torch.cat(
                    (stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0
                )
                stored_state["exp_avg_sq"] = torch.cat(
                    (stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)),
                    dim=0,
                )

                del self.optimizer.state[group["params"][0]]
                group["params"][0] = nn.Parameter(
                    torch.cat(
                        (group["params"][0], extension_tensor), dim=0
                    ).requires_grad_(True)
                )
                self.optimizer.state[group["params"][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(
                    torch.cat(
                        (group["params"][0], extension_tensor), dim=0
                    ).requires_grad_(True)
                )
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(
            self,
            new_xyz,
            new_features_dc,
            new_features_rest,
            new_opacities,
            new_scaling,
            new_rotation,
    ):

        d = {
            "xyz": new_xyz,
            "f_dc": new_features_dc,
            "f_rest": new_features_rest,
            "opacity": new_opacities,
            "scaling": new_scaling,
            "rotation": new_rotation,
        }

        optimizable_tensors = self.cat_tensors_to_optimizer(d)
        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]

        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def densify_and_split(self, grads, grad_threshold, scene_extent, N=2):
        n_init_points = self.get_xyz.shape[0]
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device="cuda")
        padded_grad[: grads.shape[0]] = grads.squeeze()
        selected_pts_mask = torch.where(padded_grad >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(
            selected_pts_mask,
            torch.max(self.get_scaling, dim=1).values
            > self.percent_dense * scene_extent,
        )

        stds = self.get_scaling[selected_pts_mask].repeat(N, 1)
        means = torch.zeros((stds.size(0), 3), device="cuda")
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self._rotation[selected_pts_mask]).repeat(N, 1, 1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[
            selected_pts_mask
        ].repeat(N, 1)
        new_scaling = self.scaling_inverse_activation(
            self.get_scaling[selected_pts_mask].repeat(N, 1) / (0.8 * N)
        )
        new_rotation = self._rotation[selected_pts_mask].repeat(N, 1)
        new_features_dc = self._features_dc[selected_pts_mask].repeat(N, 1, 1)
        new_features_rest = self._features_rest[selected_pts_mask].repeat(N, 1, 1)
        new_opacity = self._opacity[selected_pts_mask].repeat(N, 1)

        self.densification_postfix(
            new_xyz,
            new_features_dc,
            new_features_rest,
            new_opacity,
            new_scaling,
            new_rotation,
        )

        new_mask = torch.cat([self.mask[selected_pts_mask]] * N, dim=0)
        self.mask = torch.cat([self.mask, new_mask], dim=0)

        prune_filter = torch.cat(
            (
                selected_pts_mask,
                torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool),
            )
        )

        self.prune_points(prune_filter)

    def densify_and_clone(self, grads, grad_threshold, scene_extent):
        # Extract points that satisfy the gradient condition
        # import pdb; pdb.set_trace()
        selected_pts_mask = torch.where(
            torch.norm(grads, dim=-1) >= grad_threshold, True, False
        )
        selected_pts_mask = torch.logical_and(
            selected_pts_mask,
            torch.max(self.get_scaling, dim=1).values
            <= self.percent_dense * scene_extent,
        )

        new_xyz = self._xyz[selected_pts_mask]
        new_features_dc = self._features_dc[selected_pts_mask]
        new_features_rest = self._features_rest[selected_pts_mask]
        new_opacities = self._opacity[selected_pts_mask]
        new_scaling = self._scaling[selected_pts_mask]
        new_rotation = self._rotation[selected_pts_mask]

        self.densification_postfix(
            new_xyz,
            new_features_dc,
            new_features_rest,
            new_opacities,
            new_scaling,
            new_rotation,
        )
        assert (
                len(torch.nonzero(self.mask[selected_pts_mask] == 0)) == 0
        ), "nontarget area should not be densified"
        self.mask = torch.cat([self.mask, self.mask[selected_pts_mask]], dim=0)

    def densify_and_prune(
            self, max_grad, min_opacity, extent, max_screen_size, 
            is_first_densification=False, k_percent=0.15, attn_thres=0.1
    ):  

        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0
        
        self.densi_mask = self.mask > 0.5

        try:
            grads[~self.densi_mask] = 0.0
        except:
            grads = grads * self.mask[:, None]
        
        if is_first_densification: # fist densification -> prune points high top k gradient points
            print(f"first densification...\nTrimming top {k_percent}% gradient points")
            before = self.get_xyz.shape[0]
            top_k = int((k_percent / 100) * self.mask.numel())
            print("Pruning top k points: ", top_k)
            _, topk_idx = torch.topk(self.mask, k=top_k, dim=0) # device = cuda
            k_mask = torch.zeros_like(self.mask).to('cuda')
            k_mask[topk_idx]=True
            prune_mask = k_mask.bool()

            # print(
            #     f"Masked points: {(self.mask > attn_thres).sum()} / {self.mask.shape[0]}"
            # )

            self.prune_points(prune_mask)
            prune = self.get_xyz.shape[0]
            
            # print(
            #     f"Pruning: before: {before} - after: {prune}"
            # )

            self.remove_grad_mask()

            self.mask = self.mask > attn_thres
            self.apply_grad_mask(self.mask)

            torch.cuda.empty_cache()
            return

        before = self.get_xyz.shape[0]
        self.densify_and_clone(grads, max_grad, extent)
        clone = self.get_xyz.shape[0]
        self.densify_and_split(grads, max_grad, extent)
        split = self.get_xyz.shape[0]

        prune_mask = (self.get_opacity < min_opacity).squeeze()
        if max_screen_size:
            big_points_vs = self.max_radii2D > max_screen_size
            big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
            prune_mask = torch.logical_or(
                torch.logical_or(prune_mask, big_points_vs), big_points_ws
            )        

        prune_mask = torch.logical_and(prune_mask, self.mask)  # fix bug
        self.prune_points(prune_mask)
        prune = self.get_xyz.shape[0]

        # print(
        #     f"before: {before} - clone: {clone} - split: {split} - prune: {prune} "
        # )

        self.remove_grad_mask()
        self.apply_grad_mask(self.mask)

        torch.cuda.empty_cache()

    def add_densification_stats(self, viewspace_point_tensor, update_filter):
        self.xyz_gradient_accum[update_filter] += torch.norm(
            viewspace_point_tensor[update_filter, :2], dim=-1, keepdim=True
        )
        self.denom[update_filter] += 1

    def apply_weights(self, camera, weights, weights_cnt, image_weights):
        rasterizer = camera2rasterizer(
            camera, torch.tensor([0.0, 0.0, 0.0], dtype=torch.float32, device="cuda")
        )
        rasterizer.apply_weights(
            self.get_xyz,
            None,
            self.get_opacity,
            None,
            weights,
            self.get_scaling,
            self.get_rotation,
            None,
            weights_cnt,
            image_weights,
        )

    def set_mask(self, mask):
        self.mask = mask
    
    def set_densi_attn_mask(self, mask):
        self.densi_attn_mask = mask

    def set_attn_mask(self, mask):
        self.attn_mask = mask
    
    def apply_grad_mask(self, mask, l_color=1.0, l_position=1.0):
        assert self.mask.shape[0] == self._xyz.shape[0]
        self.set_mask(mask)

        def position_hook(grad):
            
            final_grad = l_position * grad *(
                self.mask[:, None] if grad.ndim == 2 else self.mask[:, None, None]
            )
            return final_grad
        
        def color_hook(grad):
            
            final_grad = l_color * grad * (
                self.mask[:, None] if grad.ndim == 2 else self.mask[:, None, None]
            )
            return final_grad
        
        fields = ["_xyz", "_features_dc", "_features_rest", "_opacity", "_scaling", "_rotation"]
        
        self.hooks = []

        for field in fields:
            this_field = getattr(self, field)
            assert this_field.is_leaf and this_field.requires_grad

            if field == "_features_dc" or field == "_features_rest" or field == "_opacity":
                self.hooks.append(this_field.register_hook(color_hook))
            else: # _xyz, _scaling, _rotation
                self.hooks.append(this_field.register_hook(position_hook))


    def remove_grad_mask(self):
        # assert hasattr(self, "hooks")
        for hook in self.hooks:
            hook.remove()

        del self.hooks

    def get_near_gaussians_by_mask(
            self, mask, dist_thresh: float = 0.1
    ):
        mask = mask.squeeze()
        object_xyz = self._xyz[mask]
        remaining_xyz = self._xyz[~mask]

        bbox_3D = torch.stack([torch.quantile(object_xyz[:, 0], 0.03), torch.quantile(object_xyz[:, 0], 0.97),
                               torch.quantile(object_xyz[:, 1], 0.03), torch.quantile(object_xyz[:, 1], 0.97),
                               torch.quantile(object_xyz[:, 2], 0.03), torch.quantile(object_xyz[:, 2], 0.97)])
        scale = bbox_3D[1::2] - bbox_3D[0::2]
        mid = (bbox_3D[1::2] + bbox_3D[0::2]) / 2
        scale *= 1.3
        bbox_3D[0::2] = mid - scale / 2
        bbox_3D[1::2] = mid + scale / 2

        in_bbox = (remaining_xyz[:, 0] >= bbox_3D[0]) & (remaining_xyz[:, 0] <= bbox_3D[1]) & \
                  (remaining_xyz[:, 1] >= bbox_3D[2]) & (remaining_xyz[:, 1] <= bbox_3D[3]) & \
                  (remaining_xyz[:, 2] >= bbox_3D[4]) & (remaining_xyz[:, 2] <= bbox_3D[5])
        in_box_remaining_xyz = remaining_xyz[in_bbox]

        _, _, nn_dist = K_nearest_neighbors(
            object_xyz, 1, query=in_box_remaining_xyz, return_dist=True
        )
        nn_dist = nn_dist.squeeze()
        valid_mask = (nn_dist <= dist_thresh)

        mask_to_update = torch.zeros_like(remaining_xyz[:, 0], dtype=torch.bool)
        true_indices = torch.nonzero(in_bbox)
        true_indices = true_indices[valid_mask, 0]
        mask_to_update[true_indices] = True

        return mask_to_update

    def concat_gaussians(self, another_gaussian):
        # return a mask
        new_xyz = another_gaussian._xyz
        new_features_dc = another_gaussian._features_dc
        new_features_rest = another_gaussian._features_rest
        new_opacities = another_gaussian._opacity
        new_scaling = another_gaussian._scaling
        new_rotation = another_gaussian._rotation
        self.densification_postfix(
            new_xyz,
            new_features_dc,
            new_features_rest,
            new_opacities,
            new_scaling,
            new_rotation,
        )
        self.mask = ~self.mask
        self.mask = torch.cat([self.mask, torch.ones_like(new_opacities[:, 0], dtype=torch.bool)], dim=0)
        self.remove_grad_mask()
        self.apply_grad_mask(self.mask)
