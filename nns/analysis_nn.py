import numpy as np
import tensorflow as tf
import tensorflow.keras.backend as K
from tensorflow.keras.layers import Conv2D, Input, Conv2DTranspose, Reshape,  Concatenate, Lambda
from tensorflow.keras.models import Model

from custom_layers import CrossCorrelationFFT, Standardize, Normalization, CropCenter

class AnalysisNNConfig:
    def __init__(self):
        # number of levels in the network (the i'th level resolution is (h/2^i, w/2^i), i=0...n_levels-1)
        self.n_levels = 4

        # the grid size of the maximal kernel we support
        # for images blurred with kernel larger than that it should be better to downsample the image, deblur and upscale it back
        self.max_kernel_size = (85, 85)

        # convolution block configuration
        self.conv_block_size = 3
        self.conv_block_n_features = 64
        self.conv_block_filter_size = 7
        self.activation = 'relu'

        # downsampling configuration
        # TODO: check if increasing the number of features improve accuracy
        self.n_downsample_features = 1
        self.downsample_filter_size = 5

        # upsampling configuration
        self.n_upsampling_features = 32
        self.upsample_filter_size = 5

        # cross correlation (cc) configuration
        self.cc_num_of_in_features = 32
        self.is_add_flips = True

        self.filter_size_after_cc = 3
        self.n_conv_filters_before_output = (24, 16, 8)

        # the maximal input size to use when predicting the kernel - in case it is smaller than the input, a window with
        # this size is cropped around the center and used for the kernel prediction (this would be fine as long as the window
        # is big enough to statistically represent the image&blur, but in general it will hurt the deblurring accuracy)
        # this is useful in case we don't have enough memory in the GPU and are getting OOME (out of memory exception)
        self.max_input_size=None


class AnalysisNN:
    def __init__(self, config=AnalysisNNConfig(), weights_path=None):
        assert np.all(np.asarray(config.max_kernel_size) % 2 == 1), f'max_kernel_size must be odd but got {config.max_kernel_size}'

        self.config = config
        self.model = self.build_model()
        if weights_path:
            self.load_weights(weights_path)

    def n_levels(self):
        return self.config.n_levels

    def load_weights(self, weights_path):
        self.model.load_weights(weights_path)

    def _crop_to_valid_size(self, images_BHWC):
        # cudaNN fail to allocate memory for a lot of input shapes, but it seems more like a bug rather than a really
        # lack of memory (it can fail for shape s1 but work fine for s2 even if s1 < s2)
        # this might not be an issue for new cuda/tf (it was for envirmoent with cuda 10.0, cudann 7.4 and tf 1.4), might've been solved in newer versions)
        # the exception seems something like
        # tensorflow/stream_executor/cuda/cuda_fft.cc:288] failed to allocate work area.
        # tensorflow/stream_executor/cuda/cuda_fft.cc:444] failed to initialize batched cufft plan with customized allocator:
        # Note that for big enough input we do get OOEM because we are really out of memory (the cross correlation part takes
        # quite a lot of memory due to large intermediate number of channels)

        # list of valid input shapes (where the allocation error wasn't thrown)
        # this list was generated by running the the network in a loop with different input sizes and taking those where no exception was thrown
        # this is of course is a partial list and not a real solution for this issue (in addition to be dependent on the abmount of GPU memory)
        known_valid_shapes = np.asarray([
            (256,  256), (240, 368), (354, 640), (368, 640), (240, 368), (384, 540), (408, 608), (408, 688), (480, 368),
            (480, 732), (492, 704), (512, 512), (512, 448), (512, 672), (512, 768), (528, 638), (528, 558), (528, 780),
            (540, 800), (558, 864), (576, 688), (576, 800), (592, 462), (592, 800), (608, 800), (608, 1012), (624, 414),
            (624, 448), (624, 576), (624, 800), (656, 1012), (672, 800),(672, 1012), (700, 1012), (704, 608), (720, 958),
            (720, 1072), (732, 1080), (750, 908), (800, 800), (800, 968), (810, 942)]
        )

        # using only part of the image in case we don't have enough GPU memory
        if self.config.max_input_size is not None:
            max_input_size = self.config.max_input_size
            if np.isscalar(max_input_size):
                max_input_size = (max_input_size, max_input_size)
            elif len(max_input_size) == 1:
                max_input_size = (max_input_size[0], max_input_size[0])

            known_valid_shapes = known_valid_shapes[known_valid_shapes.prod(axis=1) <= np.prod(max_input_size)]

        b, h, w, c = images_BHWC.shape

        # if (h, w) is valid then (w, h) should also be valid
        known_valid_shapes = np.vstack([known_valid_shapes, known_valid_shapes[:, ::-1]])

        # we only look at shapes which are smaller than the input size
        candidates_shapes = known_valid_shapes[(known_valid_shapes <= (h, w)).all(axis=1)]

        # taking the "valid" size which is the closest to the input spatial size
        normalized_diffs = ((h, w) - candidates_shapes)/(h, w)
        dists = np.linalg.norm(normalized_diffs, axis=1)
        crop_h, crop_w = candidates_shapes[np.argmin(dists)]

        starty = (h - crop_h) // 2
        startx = (w - crop_w) // 2

        return images_BHWC[:, starty:starty + crop_h, startx:startx + crop_w, :]

    def predict(self, images_BHWC, batch_size=1, **predict_kwargs):
        assert images_BHWC.ndim == 4, f'Images must be in BHWC shape, but got shape {images_BHWC.shape}'
        cropped_images_BHWC = self._crop_to_valid_size(images_BHWC)
        return self.model.predict(
            cropped_images_BHWC,
            batch_size=batch_size,
            **predict_kwargs
        )

    def build_model(self):
        c = self.config
        # the maximal shift in each axis
        max_shift_y, max_shift_x = c.max_kernel_size[0] // 2, c.max_kernel_size[1] // 2

        image_input = Input(shape=(None, None, 3))

        x = image_input

        # converting the input the grayscale (the blur should be the same across color channels)
        x = Lambda(lambda x: tf.image.rgb_to_grayscale(x))(x)
        # standardize the input to mean 0 and std 1 to reduce sensitivity to additive biases /multiplicative factors
        x = Standardize(axes=[1, 2])(x)

        prev_level_input = x

        # the cross correlation features maps for each resolution level
        cc_feature_maps_per_layer = []

        # Creating cross correlation features maps for levels i...n_levels-1 resolutions
        # the maps are latter merged together to create the estimated kernel
        for i in range(c.n_levels):
            for i in range(c.conv_block_size):
                x = Conv2D(c.conv_block_n_features, c.conv_block_filter_size, strides=1, padding='valid', activation=c.activation)(x)

            x = Conv2D(c.cc_num_of_in_features, (1, 1), strides=1, activation=c.activation, padding='valid')(x)
            x = CrossCorrelationFFT(max_shift_y, max_shift_x, c.is_add_flips)(x)

            # TODO: the reduction in #channels is VERY steep (from ~c.cc_num_of_in_features^2 to c.n_upsampling_features),
            #       a more gradual reduction would probably be beneficial (and shouldn't cost a lot in term of memory/time/#params)
            x = Conv2D(c.n_upsampling_features, (1, 1), strides=1, padding='same', activation=c.activation)(x)

            cc_feature_maps_per_layer.append(x)

            if i < c.n_levels-1:
                # TODO: currently sampling is done on the input of each level, i.e. before passing it through any feaures extractions
                #       layers (convs), need to check if sampling the output after the convs is beneficial
                x = Conv2D(c.n_downsample_features, c.downsample_filter_size, strides=2, padding='same')(prev_level_input)

                prev_level_input = x
                max_shift_y, max_shift_x = int(np.ceil(max_shift_y/2)),  int(np.ceil(max_shift_x/2))

        # combining the results from all levels (resolutions)
        x = cc_feature_maps_per_layer[-1]
        for cc_feature_maps in reversed(cc_feature_maps_per_layer[:-1]):
            x = Conv2DTranspose(c.n_upsampling_features, c.upsample_filter_size, strides=2, padding='valid', activation=c.activation)(x)

            s = K.int_shape(cc_feature_maps)
            x = CropCenter(s[1], s[2])(x)
            x = Concatenate()([cc_feature_maps, x])

            # TODO: check if adding more than one convolution here is helpful
            x = Conv2D(c.n_upsampling_features, c.filter_size_after_cc, strides=1, padding='same', activation=c.activation)(x)

        for f in c.n_conv_filters_before_output:
            x = Conv2D(f, c.filter_size_after_cc, padding='same', activation=c.activation)(x)

        x = Conv2D(1, (5, 5), padding='same', activation='relu')(x)

        # removes the extra channel dim (shape was BxKHxKWx1)
        x = Reshape(c.max_kernel_size)(x)

        # a valid kernel persevere the image energy
        out = Normalization(1)(x)

        #TODO: should we add a layer to force the kernel to be centered?
        return Model(inputs=image_input, outputs=out)

