[![Build Status](https://travis-ci.com/jpata/hepaccelerate-cms.svg?branch=master)](https://travis-ci.com/jpata/hepaccelerate-cms)

The following benchmarks have been extracted for the full Run 2 analysis as of April 30, 2020. The jobs were configured with 2 threads.

site        | Job runtime  | Number of jobs | avg. speed (ev/s) |
------------|--------------|----------------|-------------------|
Caltech T2  | 44 +- 30 min | 341            | 1.3 kHz           |
Caltech HPC | 27 +- 5 min  | 341            | 1.8 kHz           |

# hepaccelerate-cms

CMS-specific (optionally) GPU-accelerated analysis code based on the [hepaccelerate](https://github.com/hepaccelerate/hepaccelerate) backend library.

Currently implemented analyses:
- `tests/hmm/analysis_hmumu.py`: CMS-HIG-19-006, [internal](http://cms.cern.ch/iCMS/analysisadmin/cadilines?line=HIG-19-006&tp=an&id=2254&ancode=HIG-19-006)

Variations of this code have been tested at:
- T2_US_Caltech (jpata, nlu)
- Caltech HPC, http://www.hpc.caltech.edu/ (jpata)
- T2_US_Purdue
- T3_CH_PSI

This code relies on NanoAOD files being available on the local filesystem for the best performance. It is possible to use xrootd, but currently, this is not the primary focus in the interest of maximum throughput, and thus is not officially supported. The full NanoAOD for a Run 2 analysis is on the order of 5TB (1.6TB skimmed), which is generally feasible to store on local disk.

## Installation on lxplus

This code can be tested on lxplus, with the input files located on `/eos/cms/store`.
~~~
#Create the python environment
python3 -m venv venv-hepaccelerate
source venv-hepaccelerate/bin/activate
pip3 install awkward uproot numba tqdm lz4 cloudpickle scipy pyyaml cffi six tensorflow psutil xxhash keras

#Get the code
git clone https://github.com/jpata/hepaccelerate-cms.git
cd hepaccelerate-cms
git submodule init
git submodule update

#Compile the C++ helper code (Rochester corrections and lepton sf, ROOT is needed)
cd tests/hmm/
make
cd ../..

#Run the code on a few NanoAOD files from EOS
./tests/hmm/run_lxplus.sh
~~~

## Installation on Caltech T2 or GPU machine

On Caltech, an existing singularity image can be used to get the required python libraries.
~~~
git clone https://github.com/jpata/hepaccelerate-cms.git
cd hepaccelerate-cms
git submodule init
git submodule update

#Compile the C++ helpers
cd tests/hmm
singularity exec /storage/user/jpata/gpuservers/singularity/images/cupy.simg make -j4
cd ../..

#Run the code as a small test (small subset of the data by default, edit the file to change this)
#This should take approximately 10 minutes and processes 1 file from each dataset for each year
./tests/hmm/run.sh
~~~

## Installation on Caltech HPC

```bash
#Go through the Caltech Tier2
ssh USER@login-1.hep.caltech.edu

#copy the skimmed samples to HPC, needs to be done once, or as top-up if you update the skim
#use the rsync /./ and -R option to copy the full path structure, --ignore-existing to update just the new files
rsync -rR --progress --ignore-existing /storage/user/nlu/./hmm/skim_merged USER@login1.hpc.caltech.edu:/central/groups/smaria/

#now log in to the HPC machine
ssh USER@login1.hpc.caltech.edu 

#activate the prepared python environment (should you wish to do this from scratch, you can use the example in environments/setup-miniconda.sh)
source /central/groups/smaria/jpata/miniconda3/bin/activate

#make a working directory under the shared filesystem
mkdir /central/groups/smaria/$USER
cd /central/groups/smaria/$USER
mkdir hmm
cd hmm

#get the code, compile the C++ helper library
git clone https://github.com/jpata/hepaccelerate-cms
git submodule init
git submodule update
cd hepaccelerate-cms/tests/hmm
make -j4
cd ../..

#run a local test on the interactive login node and create jobfiles
./tests/hmm/run_hpc.sh
export SUBMIT_DIR=`pwd`

#submit batch jobs
cd batch
./make_submit_jdl.sh
source slurm_submit.sh

#monitor the jobs
squeue -u $USER

#check the output
python verify_analyze.py slurm_submit.sh
python check_logs.py "slurm-*.out"

#submit the merge job
sbatch slurm_post.sh
```

## Running on full dataset using batch queue
We use the condor batch queue on Caltech T2 to run the analysis. It takes ~20 minutes for all 3 years using just the Total JEC & JER (2-3h using factorized JEC) using about 200 job slots.

~~~
#Submit batch jobs after this step is successful
mkdir /storage/user/$USER/hmm
export SUBMIT_DIR=`pwd`

#Prepare the list of datasets (out/datasets.json) and the jobfiles (out/jobfiles/*.json) 
./tests/hmm/run.sh

cd batch
mkdir logs

#Run the NanoAOD skimming step (cache creation).
#This is quite heavy (~6h total), so do this only
#when adding new samples
./make_cache_jdl.sh
condor_submit cache.jdl
#...wait until done, create resubmit file if needed
python verify_cache.py
du -csh ~/hmm/skim_merged

#Now run the analysis, this can be between 20 minutes and a few hours
./make_submit_jdl.sh
condor_submit analyze.jdl
#...wait until done, create resubmit file if needed
python verify_analyze.py args_analyze.txt
du -csh ~/hmm/out_*.tgz

#submit merging and plotting, this should be around 30 minutes
condor_submit merge.jdl
du -csh ~/hmm/out

cd ..

#when all was successful, delete partial results
rm -Rf /storage/user/$USER/hmm/out/partial_results
du -csh /storage/user/$USER/hmm/out
~~~

## Making plots, datacards and histograms
From the output results, one can make datacards and plots by executing this command:
~~~
./tests/hmm/plots.sh out
~~~
This creates a directory called `baseline` which has the datacards and plots. This can also be run on the batch using `merge.jdl`.

# Contributing
If you use this code, we are happy to consider issues and merge improvements.
- Please make an issue on the Issues page for any bugs you find.
- To contribute changes, please use the 'Fork and Pull' model: https://reflectoring.io/github-fork-and-pull.
- For non-trivial pull requests, please ask at least one other person with push access to review the changes.

# Misc notes
Luminosity, details on how to set up on this [link](https://cms-service-lumi.web.cern.ch/cms-service-lumi/brilwsdoc.html).
~~~
export PATH=$HOME/.local/bin:/cvmfs/cms-bril.cern.ch/brilconda/bin:$PATH
brilcalc lumi -c /cvmfs/cms.cern.ch/SITECONF/local/JobConfig/site-local-config.xml \
    -b "STABLE BEAMS" --normtag=/cvmfs/cms-bril.cern.ch/cms-lumi-pog/Normtags/normtag_PHYSICS.json \
    -u /pb --byls --output-style csv -i /afs/cern.ch/cms/CAF/CMSCOMM/COMM_DQM/certification/Collisions16/13TeV/ReReco/Final/Cert_271036-284044_13TeV_23Sep2016ReReco_Collisions16_JSON.txt > lumi2016.csv

brilcalc lumi -c /cvmfs/cms.cern.ch/SITECONF/local/JobConfig/site-local-config.xml \
    -b "STABLE BEAMS" --normtag=/cvmfs/cms-bril.cern.ch/cms-lumi-pog/Normtags/normtag_PHYSICS.json \
    -u /pb --byls --output-style csv -i /afs/cern.ch/cms/CAF/CMSCOMM/COMM_DQM/certification/Collisions17/13TeV/ReReco/Cert_294927-306462_13TeV_EOY2017ReReco_Collisions17_JSON_v1.txt > lumi2017.csv

brilcalc lumi -c /cvmfs/cms.cern.ch/SITECONF/local/JobConfig/site-local-config.xml \
    -b "STABLE BEAMS" --normtag=/cvmfs/cms-bril.cern.ch/cms-lumi-pog/Normtags/normtag_PHYSICS.json \
    -u /pb --byls --output-style csv -i /afs/cern.ch/cms/CAF/CMSCOMM/COMM_DQM/certification/Collisions18/13TeV/ReReco/Cert_314472-325175_13TeV_17SeptEarlyReReco2018ABC_PromptEraD_Collisions18_JSON.txt > lumi2018.csv
~~~
