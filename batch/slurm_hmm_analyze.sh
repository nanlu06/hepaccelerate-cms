#!/bin/bash

#Submit this script with: sbatch thefilename

#SBATCH --time=4:00:00   # walltime
#SBATCH --ntasks=2   # number of processor cores (i.e. tasks)
#SBATCH --nodes=1   # number of nodes
#SBATCH --mem-per-cpu=2G   # memory per CPU core
#SBATCH -J "hmm"   # job name

set -e

export NTHREADS=2

export NUMBA_NUM_THREADS=$NTHREADS
export OMP_NUM_THREADS=$NTHREADS
export NUMBA_THREADING_LAYER=omp
export WORKDIR=/central/groups/smaria/$USER/hmm/hepaccelerate-cms
export CACHEPATH=/central/groups/smaria/hmm/skim_merged
export JOB_TMPDIR=$TMPDIR/$SLURM_JOB_ID
export OUTDIR=out

export OUTFILE=$1
export INFILE=$2

env

mkdir $JOB_TMPDIR
cd $JOB_TMPDIR

cp -R $SUBMIT_DIR/batch/jobfiles ./

mkdir $OUTDIR
mv jobfiles/datasets.json $OUTDIR/
mv jobfiles $OUTDIR/

#rename the input files to fit Caltech HPC
python3 $SUBMIT_DIR/batch/addprefix.py $JOB_TMPDIR/$OUTDIR/ < $OUTDIR/$INFILE > $OUTDIR/$INFILE.tmp
mv $OUTDIR/$INFILE.tmp $OUTDIR/$INFILE

cd $SUBMIT_DIR

PYTHONPATH=hepaccelerate:coffea:. python3 tests/hmm/analysis_hmumu.py \
    --action analyze \
    --cachepath $CACHEPATH \
    --nthreads $NTHREADS \
    --do-factorized-jec \
    --do-fsr \
    --datasets-yaml data/datasets_NanoAODv6_Run2_mixv1.yml \
    --jobfiles-load $JOB_TMPDIR/$OUTDIR/$INFILE \
    --out $JOB_TMPDIR/$OUTDIR

cd $JOB_TMPDIR
tar -czf out.tgz $OUTDIR
rsync out.tgz $OUTFILE

cd $SUBMIT_DIR

rm -Rf $JOB_TMPDIR
