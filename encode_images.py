import os
import argparse
import pickle
from tqdm import tqdm
import PIL.Image
import numpy as np
import dnnlib
import dnnlib.tflib as tflib
import config
from encoder.generator_model import Generator
from encoder.percmod_oneiro import PerceptualModel, load_images
from keras.models import load_model

def split_to_batches(l, n):
    for i in range(0, len(l), n):
        yield l[i:i + n]

def main():
    parser = argparse.ArgumentParser(description='Find latent representation of reference images using perceptual losses', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('src_dir', help='Directory with images for encoding')
    parser.add_argument('generated_images_dir', help='Directory for storing generated images')
    parser.add_argument('dlatent_dir', help='Directory for storing dlatent representations')
    parser.add_argument('--data_dir', default='data', help='Directory for storing optional models')
    parser.add_argument('--mask_dir', default='masks', help='Directory for storing optional masks')
    parser.add_argument('--load_last', default='', help='Start with embeddings from directory')
    parser.add_argument('--dlatent_avg', default='', help='Use dlatent from file specified here for truncation instead of dlatent_avg from Gs')
    parser.add_argument('--model_url', default='https://drive.google.com/uc?id=1MEGjdvVpUsu1jB4zrXZN7Y4kBBOzizDQ', help='Fetch a StyleGAN model to train on from this URL') # karras2019stylegan-ffhq-1024x1024.pkl
    parser.add_argument('--model_res', default=1024, help='The dimension of images in the StyleGAN model', type=int)
    parser.add_argument('--batch_size', default=1, help='Batch size for generator and perceptual model', type=int)

    # Perceptual model params
    parser.add_argument('--image_size', default=512, help='Size of images for perceptual model', type=int)
    parser.add_argument('--resnet_image_size', default=256, help='Size of images for the Resnet model', type=int)
    parser.add_argument('--lr', default=0.02, help='Learning rate for perceptual model', type=float)
    parser.add_argument('--decay_rate', default=0.9, help='Decay rate for learning rate', type=float)
    parser.add_argument('--iterations', default=500, help='Number of optimization steps for each batch', type=int)
    parser.add_argument('--decay_steps', default=10, help='Decay steps for learning rate decay (as a percent of iterations)', type=float)
    parser.add_argument('--load_effnet', default='data/finetuned_effnet.h5', help='Model to load for EfficientNet approximation of dlatents')
    parser.add_argument('--load_resnet', default='data/finetuned_resnet.h5', help='Model to load for ResNet approximation of dlatents')
    parser.add_argument('--fn_model_path', default='data/20180408-102900.pb', help='Model FN')

    # Loss function options
    parser.add_argument('--use_fn_loss', default=100., help='Use VGG perceptual loss; 0 to disable, > 0 to scale.', type=float)
    parser.add_argument('--use_vgg_layer', default=9, help='Pick which VGG layer to use.', type=int)
    parser.add_argument('--use_pixel_loss', default=1.5, help='Use logcosh image pixel loss; 0 to disable, > 0 to scale.', type=float)
    parser.add_argument('--use_mssim_loss', default=0, help='Use MS-SIM perceptual loss; 0 to disable, > 0 to scale.', type=float)
    parser.add_argument('--use_lpips_loss', default=0, help='Use LPIPS perceptual loss; 0 to disable, > 0 to scale.', type=float)
    parser.add_argument('--use_l1_penalty', default=0.01, help='Use L1 penalty on latents; 0 to disable, > 0 to scale.', type=float)

    # Generator params
    parser.add_argument('--randomize_noise', default=False, help='Add noise to dlatents during optimization', type=bool)
    parser.add_argument('--tile_dlatents', default=False, help='Tile dlatents to use a single vector at each scale', type=bool)
    parser.add_argument('--clipping_threshold', default=2.0, help='Stochastic clipping of gradient values outside of this threshold', type=float)

    # Masking params
    parser.add_argument('--load_mask', default=False, help='Load segmentation masks', type=bool)
    parser.add_argument('--face_mask', default=False, help='Generate a mask for predicting only the face area', type=bool)
    parser.add_argument('--use_grabcut', default=True, help='Use grabcut algorithm on the face mask to better segment the foreground', type=bool)
    parser.add_argument('--scale_mask', default=1.5, help='Look over a wider section of foreground for grabcut', type=float)

    # Video params
    parser.add_argument('--video_dir', default='videos', help='Directory for storing training videos')
    parser.add_argument('--output_video', default=True, help='Generate videos of the optimization process', type=bool)
    parser.add_argument('--video_codec', default='MJPG', help='FOURCC-supported video codec name')
    parser.add_argument('--video_frame_rate', default=24, help='Video frames per second', type=int)
    parser.add_argument('--video_size', default=256, help='Video size in pixels', type=int)
    parser.add_argument('--video_skip', default=1, help='Only write every n frames (1 = write every frame)', type=int)

    args, other_args = parser.parse_known_args()

    args.decay_steps *= 0.01 * args.iterations # Calculate steps as a percent of total iterations

    if args.output_video:
      import cv2
      synthesis_kwargs = dict(output_transform=dict(func=tflib.convert_images_to_uint8, nchw_to_nhwc=False), minibatch_size=args.batch_size)

    ref_images = [os.path.join(args.src_dir, x) for x in os.listdir(args.src_dir)]
    ref_images = list(filter(os.path.isfile, ref_images))

    if len(ref_images) == 0:
        raise Exception('%s is empty' % args.src_dir)

    os.makedirs(args.data_dir, exist_ok=True)
    os.makedirs(args.mask_dir, exist_ok=True)
    os.makedirs(args.generated_images_dir, exist_ok=True)
    os.makedirs(args.dlatent_dir, exist_ok=True)
    os.makedirs(args.video_dir, exist_ok=True)

    # Initialize generator and perceptual model
    tflib.init_tf()
    with open(args.model_url, 'rb') as fp:
        Gs_network = pickle.load(fp)

    generator = Generator(Gs_network, args.batch_size, tiled_dlatent=args.tile_dlatents, model_res=args.model_res, randomize_noise=args.randomize_noise)
    if (args.dlatent_avg != ''):
        generator.set_dlatent_avg(np.load(args.dlatent_avg))

    perc_model = None
    if (args.use_lpips_loss > 0.00000001):
        with dnnlib.util.open_url('https://drive.google.com/uc?id=1N2-m9qszOeVC9Tq77WxsLnuWwOedQiD2', cache_dir=config.cache_dir) as f:
            perc_model =  pickle.load(f)
    perceptual_model = PerceptualModel(args, perc_model=perc_model, batch_size=args.batch_size)
    perceptual_model.build_perceptual_model(generator)

    ff_model = None

    # Optimize (only) dlatents by minimizing perceptual loss between reference and generated images in feature space

    images_batch = ref_images
    names = [os.path.splitext(os.path.basename(x))[0] for x in images_batch]
    if args.output_video:
      video_out = {}
      for name in names:
        video_out[name] = cv2.VideoWriter(os.path.join(args.video_dir, f'{name}.avi'),cv2.VideoWriter_fourcc(*args.video_codec), args.video_frame_rate, (args.video_size,args.video_size))

    perceptual_model.set_reference_images(images_batch)
    dlatents = None
    if (args.load_last != ''): # load previous dlatents for initialization
        for name in names:
            dl = np.expand_dims(np.load(os.path.join(args.load_last, f'{name}.npy')),axis=0)
            if (dlatents is None):
                dlatents = dl
            else:
                dlatents = np.vstack((dlatents,dl))
    else:
        if (ff_model is None):
            if os.path.exists(args.load_resnet):
                print("Loading ResNet Model:")
                ff_model = load_model(args.load_resnet)
                from keras.applications.resnet50 import preprocess_input
        if (ff_model is None):
            if os.path.exists(args.load_effnet):
                import efficientnet
                print("Loading EfficientNet Model:")
                ff_model = load_model(args.load_effnet)
                from efficientnet import preprocess_input
        if (ff_model is not None): # predict initial dlatents with ResNet model
            dlatents = ff_model.predict(preprocess_input(load_images(images_batch,image_size=args.resnet_image_size)))
    if dlatents is not None:
        generator.set_dlatents(dlatents)

    fetch_ops = perceptual_model.get_fetch_ops(generator.dlatent_variable)
    vid_count = 0
    best_loss = None
    best_dlatent = None
    for idx_iter in range(args.iterations):
        res = perceptual_model.sess.run(fetch_ops)
        _, loss, lr = res
        if best_loss is None or loss < best_loss:
            best_loss = loss
            best_dlatent = generator.get_dlatents()
        if args.output_video and (vid_count % args.video_skip == 0):
            batch_frames = generator.generate_images()
            for i, name in enumerate(names):
                video_frame = PIL.Image.fromarray(batch_frames[i], 'RGB').resize((args.video_size, args.video_size),
                                                                                 PIL.Image.LANCZOS)
                video_out[name].write(cv2.cvtColor(np.array(video_frame).astype('uint8'), cv2.COLOR_RGB2BGR))

    '''
    op = perceptual_model.optimize(generator.dlatent_variable, iterations=args.iterations)
    vid_count = 0
    best_loss = None
    best_dlatent = None
    iternow=0
    for loss_dict in op:
        #descr = " ".join(names) + ": " + "; ".join(["{} {:.4f}".format(k, v) for k, v in loss_dict.items()])
        print(loss_dict)
        print(iternow)
        iternow += 1
        if best_loss is None or loss_dict["loss"] < best_loss:
            best_loss = loss_dict["loss"]
            best_dlatent = generator.get_dlatents()
        if args.output_video and (vid_count % args.video_skip == 0):
            batch_frames = generator.generate_images()
            for i, name in enumerate(names):
                video_frame = PIL.Image.fromarray(batch_frames[i], 'RGB').resize((args.video_size, args.video_size),
                                                                                 PIL.Image.LANCZOS)
                video_out[name].write(cv2.cvtColor(np.array(video_frame).astype('uint8'), cv2.COLOR_RGB2BGR))

        #generator.stochastic_clip_dlatents()

    #print(" ".join(names), " Loss {:.4f}".format(best_loss))
    '''

    if args.output_video:
        for name in names:
            video_out[name].release()

    # Generate images from found dlatents and save them
    generator.set_dlatents(best_dlatent)
    generated_images = generator.generate_images()
    generated_dlatents = generator.get_dlatents()
    for img_array, dlatent, img_name in zip(generated_images, generated_dlatents, names):
        img = PIL.Image.fromarray(img_array, 'RGB')
        img.save(os.path.join(args.generated_images_dir, f'{img_name}.png'), 'PNG')
        np.save(os.path.join(args.dlatent_dir, f'{img_name}.npy'), dlatent)

    generator.reset_dlatents()


if __name__ == "__main__":
    main()
