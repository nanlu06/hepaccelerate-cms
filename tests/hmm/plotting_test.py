import numpy as np
import os
from hepaccelerate.utils import Histogram, Results

from collections import OrderedDict
import uproot


import copy
import multiprocessing

from pars import catnames, varnames, analysis_names, shape_systematics, controlplots_shape, genweight_scalefactor, lhe_pdf_variations
from pars import process_groups, colors, extra_plot_kwargs,proc_grps,combined_signal_samples, remove_proc

from scipy.stats import wasserstein_distance
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from cmsutils.stats import kolmogorov_smirnov

import argparse
import pickle
import glob

import cloudpickle
import json
import yaml

if __name__ == "__main__":

	with open('/storage/user/nlu/hmm/April28_pr96_binning/results/vbf_amc_herwig_125_2016.pkl', 'rb') as f:
    		data = pickle.load(f)
    		print(data["BBEC1__up"])
