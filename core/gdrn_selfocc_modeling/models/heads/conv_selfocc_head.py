import torch.nn as nn
from torch.nn.modules.batchnorm import _BatchNorm
from mmcv.cnn import normal_init, constant_init
from core.gdrn_selfocc_modeling.tools.layers.layer_utils import get_norm, get_nn_act_func
from core.gdrn_selfocc_modeling.tools.layers.conv_module import ConvModule


class ConvSelfoccHead(nn.Module):
    def __init__(
        self,
        in_dim,
        up_types=("deconv", "bilinear", "bilinear"),
        deconv_kernel_size=3,
        num_conv_per_block=2,
        feat_dim=256,
        feat_kernel_size=3,
        norm="GN",
        num_gn_groups=32,
        act="GELU",
        out_kernel_size=1,

        occmask_aware=False,
        out_layer_shared=True,
        mask_num_classes=1,
        Q0_num_classes=1,

        occmask_out_dim=3,
        Q0_out_dim=6,

    ):
        """
        Args:
            up_types: use up-conv or deconv for each up-sampling layer
                ("bilinear", "bilinear", "bilinear")
                ("deconv", "bilinear", "bilinear")  # CDPNv2 rot head
                ("deconv", "deconv", "deconv")  # CDPNv1 rot head
                ("nearest", "nearest", "nearest")  # implement here but maybe won't use
        NOTE: default from stride 32 to stride 4 (3 ups)
        """
        super().__init__()
        assert out_kernel_size in [1, 3], "Only support output kernel size: 1 and 3"
        assert deconv_kernel_size in [1, 3, 4], "Only support deconv kernel size: 1, 3, and 4"
        assert len(up_types) > 0, up_types

        self.features = nn.ModuleList()
        for i, up_type in enumerate(up_types):
            _in_dim = in_dim if i == 0 else feat_dim
            if up_type == "deconv":
                deconv_kernel, deconv_pad, deconv_out_pad = _get_deconv_pad_outpad(deconv_kernel_size)
                self.features.append(
                    nn.ConvTranspose2d(
                        _in_dim,
                        feat_dim,
                        kernel_size=deconv_kernel,
                        stride=2,
                        padding=deconv_pad,
                        output_padding=deconv_out_pad,
                        bias=False,
                    )
                )
                self.features.append(get_norm(norm, feat_dim, num_gn_groups=num_gn_groups))
                self.features.append(get_nn_act_func(act))
            elif up_type == "bilinear":
                self.features.append(nn.UpsamplingBilinear2d(scale_factor=2))
            elif up_type == "nearest":
                self.features.append(nn.UpsamplingNearest2d(scale_factor=2))
            else:
                raise ValueError(f"Unknown up_type: {up_type}")

            if up_type in ["bilinear", "nearest"]:
                assert num_conv_per_block >= 1, num_conv_per_block
            for i_conv in range(num_conv_per_block):
                if i == 0 and i_conv == 0 and up_type in ["bilinear", "nearest"]:
                    conv_in_dim = in_dim
                else:
                    conv_in_dim = feat_dim
                self.features.append(
                    ConvModule(
                        conv_in_dim,
                        feat_dim,
                        kernel_size=feat_kernel_size,
                        padding=(feat_kernel_size - 1) // 2,
                        norm=norm,
                        num_gn_groups=num_gn_groups,
                        act=act,
                    )
                )

        self.out_layer_shared = out_layer_shared
        self.occmask_aware = occmask_aware

        self.mask_num_classes = mask_num_classes
        self.Q0_num_classes = Q0_num_classes

        self.mask_out_dim = occmask_out_dim
        self.Q0_out_dim = Q0_out_dim

        if self.out_layer_shared and self.occmask_aware:
            out_dim = (
                self.mask_out_dim * self.mask_num_classes
                + self.Q0_out_dim * self.Q0_num_classes
            )
            self.out_layer = nn.Conv2d(
                feat_dim,
                out_dim,
                kernel_size=out_kernel_size,
                padding=(out_kernel_size - 1) // 2,
                bias=True,
            )
        elif (not self.out_layer_shared) and self.occmask_aware:
            self.occmask_out_layer = nn.Conv2d(
                feat_dim,
                self.mask_out_dim * self.mask_num_classes,
                kernel_size=out_kernel_size,
                padding=(out_kernel_size - 1) // 2,
                bias=True,
            )
            self.Q0_out_layer = nn.Conv2d(
                feat_dim,
                self.Q0_out_dim * self.Q0_num_classes,
                kernel_size=out_kernel_size,
                padding=(out_kernel_size - 1) // 2,
                bias=True,
            )
        else:
            self.out_layer = nn.Conv2d(
                feat_dim,
                self.Q0_out_dim * self.Q0_num_classes,
                kernel_size=out_kernel_size,
                padding=(out_kernel_size - 1) // 2,
                bias=True,
            )

        # init
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                normal_init(m, std=0.001)
            elif isinstance(m, (_BatchNorm, nn.GroupNorm)):
                constant_init(m, 1)
            elif isinstance(m, nn.ConvTranspose2d):
                normal_init(m, std=0.001)
        # init output layers
        if (not self.out_layer_shared) and self.occmask_aware:
            normal_init(self.occmask_out_layer, std=0.01)
            normal_init(self.Q0_out_layer, std=0.01)
        else:
            normal_init(self.out_layer, std=0.01)

    def forward(self, x):
        if isinstance(x, (tuple, list)) and len(x) == 1:
            x = x[0]
        for i, l in enumerate(self.features):
            x = l(x)
        if self.out_layer_shared and self.occmask_aware:
            out = self.out_layer(x)
            mask_dim = self.mask_out_dim * self.mask_num_classes
            mask = out[:, :mask_dim, :, :]

            Q_dim = self.Q0_out_dim * self.Q0_num_classes
            Q = out[:, mask_dim:, :, :]

            bs, c, h, w = Q.shape
            Q0 = Q.view(bs, c, c // 6, h, w)
            Q0_yz_y = Q0[:, 0, :, :, :]
            Q0_yz_z = Q0[:, 1, :, :, :]
            Q0_xz_x = Q0[:, 2, :, :, :]
            Q0_xz_z = Q0[:, 3, :, :, :]
            Q0_xy_x = Q0[:, 4, :, :, :]
            Q0_xy_y = Q0[:, 5, :, :, :]
            return mask, Q0_yz_y, Q0_yz_z, Q0_xz_x, Q0_xz_z, Q0_xy_x, Q0_xy_y

        elif (not self.out_layer_shared) and self.occmask_aware:
            mask = self.occmask_out_layer(x)
            Q = self.Q0_out_layer(x)
            bs, c, h, w = Q.shape
            Q0 = Q.view(bs, c, c // 6, h, w)
            Q0_yz_y = Q0[:, 0, :, :, :]
            Q0_yz_z = Q0[:, 1, :, :, :]
            Q0_xz_x = Q0[:, 2, :, :, :]
            Q0_xz_z = Q0[:, 3, :, :, :]
            Q0_xy_x = Q0[:, 4, :, :, :]
            Q0_xy_y = Q0[:, 5, :, :, :]
            return mask, Q0_yz_y, Q0_yz_z, Q0_xz_x, Q0_xz_z, Q0_xy_x, Q0_xy_y

        else:
            Q = self.out_layer(x)
            bs, c, h, w = Q.shape
            Q0 = Q.view(bs, 6, c // 6, h, w)
            Q0_yz_y = Q0[:, 0, :, :, :]
            Q0_yz_z = Q0[:, 1, :, :, :]
            Q0_xz_x = Q0[:, 2, :, :, :]
            Q0_xz_z = Q0[:, 3, :, :, :]
            Q0_xy_x = Q0[:, 4, :, :, :]
            Q0_xy_y = Q0[:, 5, :, :, :]
            return Q0_yz_y, Q0_yz_z, Q0_xz_x, Q0_xz_z, Q0_xy_x, Q0_xy_y



def _get_deconv_pad_outpad(deconv_kernel):
    """Get padding and out padding for deconv layers."""
    if deconv_kernel == 4:
        padding = 1
        output_padding = 0
    elif deconv_kernel == 3:
        padding = 1
        output_padding = 1
    elif deconv_kernel == 2:
        padding = 0
        output_padding = 0
    else:
        raise ValueError(f"Not supported num_kernels ({deconv_kernel}).")

    return deconv_kernel, padding, output_padding
