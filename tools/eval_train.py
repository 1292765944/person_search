import _init_paths

import argparse
import pprint
import time, os, sys
import os.path as osp

import numpy as np
import caffe
from mpi4py import MPI

from fast_rcnn.test_gallery import detect_and_exfeat
from fast_rcnn.test_probe import exfeat
from fast_rcnn.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from datasets.factory import get_imdb
from utils import pickle, unpickle
from eval_utils import mpi_dispatch, mpi_collect


# mpi setup
mpi_comm = MPI.COMM_WORLD
mpi_size = mpi_comm.Get_size()
mpi_rank = mpi_comm.Get_rank()
if mpi_rank > 0:
    # disable print on other mpi processes
    sys.stdout = open(os.devnull, 'w')


def main(args):
    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs)

    # parse gpus
    gpus = map(int, args.gpus.split(','))
    assert len(gpus) >= mpi_size, "Number of GPUs must be >= MPI size"
    cfg.GPU_ID = gpus[mpi_rank]

    # parse feature blob names
    blob_names = args.blob_names.split(',')

    print('Using config:')
    pprint.pprint(cfg)

    while not osp.exists(args.caffemodel) and args.wait:
        print('Waiting for {} to exist...'.format(args.caffemodel))
        time.sleep(10)

    # load imdb
    imdb = get_imdb(args.imdb_name)
    root_dir = imdb._root_dir
    images_dir = imdb._data_path

    # setup caffe
    caffe.mpi_init()
    caffe.set_mode_gpu()
    caffe.set_device(cfg.GPU_ID)

    # 1. Detect and extract features from all the gallery images in the imdb
    start, end = mpi_dispatch(len(imdb.image_index), mpi_size, mpi_rank)
    net = caffe.Net(args.prototxt, args.caffemodel, caffe.TEST)
    net.name = osp.splitext(osp.basename(args.caffemodel))[0]
    gboxes, gfeatures = detect_and_exfeat(net, imdb,
        start=start, end=end, blob_names=blob_names, vis=args.vis)
    # pid_prob could be very large, so we change it to top-100 ranked pids
    if 'pid_prob' in gfeatures:
        ranks = []
        for p in gfeatures['pid_prob']:
            r = np.argsort(p, axis=1)[:, ::-1]
            r = r[:, :min(100, r.shape[1])]
            ranks.append(r)
        del gfeatures['pid_prob']
        gfeatures['pid_rank'] = ranks

    gboxes = mpi_collect(mpi_comm, mpi_rank, gboxes)
    gfeatures = mpi_collect(mpi_comm, mpi_rank, gfeatures)

    # Evaluate
    if mpi_rank == 0:
        output_dir = get_output_dir(imdb, net)
        pickle(gboxes, osp.join(output_dir, 'gallery_detections.pkl'))
        pickle(gfeatures, osp.join(output_dir, 'gallery_features.pkl'))
        imdb.evaluate_cls(gboxes, gfeatures['pid_rank'], gfeatures['pid_label'],
                          args.det_thresh)

    caffe.mpi_finalize()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Evalute on training set, including classification accuracy')
    parser.add_argument('--gpus',
                        help='comma separated GPU device ids',
                        default='0')
    parser.add_argument('--imdb', dest='imdb_name',
                        help='dataset to test',
                        default='psdb_train')
    parser.add_argument('--def', dest='prototxt',
                        help='prototxt file defining the network')
    parser.add_argument('--net', dest='caffemodel',
                        help='model to test')
    parser.add_argument('--blob_names',
                        help='comma separated names of the feature blobs ' \
                             'to be extracted',
                        default='feat,pid_label,pid_prob')
    parser.add_argument('--det_thresh',
                        help="detection score threshold to be evaluated",
                        type=float, default=0.5)
    parser.add_argument('--wait',
                        help='wait until net file exists',
                        default=True, type=bool)
    parser.add_argument('--vis',
                        help='visualize detections',
                        action='store_true')
    parser.add_argument('--cfg', dest='cfg_file',
                        help='optional config file')
    parser.add_argument('--set', dest='set_cfgs',
                        help='set config keys', default=None,
                        nargs=argparse.REMAINDER)

    args = parser.parse_args()

    print('Called with args:')
    print(args)

    main(args)