import pickle, json
import threading
import uproot
import copy
import psutil
import glob
import time
import numpy
import numpy as np
import sys
import os
import math

import numba
import numba.cuda as cuda

import threading
from threading import Thread
from queue import Queue
import queue
import concurrent.futures

import hepaccelerate
import hepaccelerate.utils
from hepaccelerate.utils import Results
from hepaccelerate.utils import Dataset
from hepaccelerate.utils import Histogram
import hepaccelerate.backend_cpu as backend_cpu

from cmsutils.decisiontree import DecisionTreeNode, DecisionTreeLeaf, make_random_node, grow_randomly, make_random_tree, prune_randomly, generate_cut_trees
from cmsutils.stats import likelihood, sig_q0_asimov, sig_naive

from pars import runmap_numerical, runmap_numerical_r, data_runs, genweight_scalefactor

#global variables need to be configured here for the hepaccelerate backend and numpy library
#they will be overwritten later
ha = None
NUMPY_LIB = None

#Use these to turn on debugging
debug = False

#event IDs for which to print out detailed information
debug_event_ids = []

#list to collect performance data in
global_metrics = []

def fix_inf_nan(data, default=0):
    data[NUMPY_LIB.isinf(data)] = default
    data[NUMPY_LIB.isnan(data)] = default

def analyze_data(
    data,
    use_cuda=False,
    is_mc=True,
    pu_corrections=None,
    lumimask=None,
    lumidata=None,
    rochester_corrections=None,
    lepsf_iso=None,
    lepsf_id=None,
    lepsf_trig=None,
    dnn_model=None,
    dnn_normfactors=None,
    dnnPisa_model=None,
    dnnPisa_normfactors1=None,
    dnnPisa_normfactors2=None,
    jetmet_corrections=None,
    parameters={},
    parameter_set_name="",
    doverify=False,
    do_sync = False,
    dataset_era = "",
    dataset_name = "",
    dataset_num_chunk = "",
    bdt_ucsd = None,
    bdt2j_ucsd = None,
    bdt01j_ucsd = None,
    miscvariables = None,
    nnlopsreweighting = None,
    hrelresolution = None 
    ):

    muons = data["Muon"]
    jets = data["Jet"]
    electrons = data["Electron"]
    trigobj = data["TrigObj"]
    scalars = data["eventvars"]
    LHEScalew = {}
    if "dy" in dataset_name or "ewk" in dataset_name:
        LHEScalew = data["LHEScaleWeight"]
    histo_bins = parameters["histo_bins"]
    mask_events = NUMPY_LIB.ones(muons.numevents(), dtype=NUMPY_LIB.bool)

    #Compute integrated luminosity on data sample and apply golden JSON
    int_lumi = compute_integrated_luminosity(scalars, lumimask, lumidata, dataset_era, mask_events, is_mc)
    check_and_fix_qgl(jets)

    #output histograms 
    hists = {}

    #temporary hack for JaggedStruct.select_objects (relies on backend)
    muons.hepaccelerate_backend = ha
    jets.hepaccelerate_backend = ha

    #associate the muon genpt to reco muons based on the NanoAOD index
    genJet, genpart = get_genparticles(data, muons, jets, is_mc, use_cuda)
    
    #NNLOPS reweighting for ggH signal
    gghnnlopsw = NUMPY_LIB.ones(muons.numevents(), dtype=NUMPY_LIB.float32)

    if is_mc and (dataset_name in parameters["ggh_nnlops_reweight"]):
        #find genHiggs
        genHiggs_mask = NUMPY_LIB.logical_and((genpart.pdgId == 25), (genpart.status == 62))
        genHiggs_pt = genhpt(muons.numevents(),genpart, genHiggs_mask)
        genNjets = gennjets(muons.numevents(),genJet, 30.0)
        gghnnlopsw = nnlopsreweighting.compute(genNjets,genHiggs_pt, parameters["ggh_nnlops_reweight"][dataset_name])

    #Find the first two genjets in the event that are not matched to gen-leptons
    mask_vbf_filter = None
    if is_mc and (dataset_name in parameters["vbf_filter"]):
        #find genleptons
        genpart_pdgid = NUMPY_LIB.abs(genpart.pdgId)
        genpart_mask = (genpart_pdgid == 11)
        genpart_mask = NUMPY_LIB.logical_or(genpart_mask, (genpart_pdgid == 13))
        genpart_mask = NUMPY_LIB.logical_or(genpart_mask, (genpart_pdgid == 15))

        genjets_not_matched_genlepton = ha.mask_deltar_first(
            genJet, genJet.masks["all"], genpart, genpart_mask, 0.3
        )
        out_genjet_mask = NUMPY_LIB.zeros(genJet.numobjects(), dtype=NUMPY_LIB.bool)
        inds = NUMPY_LIB.zeros_like(mask_events)
        targets = NUMPY_LIB.ones_like(mask_events)
        inds[:] = 0
        ha.set_in_offsets(out_genjet_mask, genJet.offsets, inds, targets, mask_events, genjets_not_matched_genlepton)
        inds[:] = 1
        ha.set_in_offsets(out_genjet_mask, genJet.offsets, inds, targets, mask_events, genjets_not_matched_genlepton)

        num_good_genjets = ha.sum_in_offsets(genJet, out_genjet_mask, mask_events, genJet.masks["all"], NUMPY_LIB.int8)

        genjet_inv_mass, _ = compute_inv_mass(genJet, mask_events, out_genjet_mask, use_cuda)
        genjet_inv_mass[num_good_genjets<2] = 0
        
        mask_vbf_filter = vbf_genfilter(genjet_inv_mass, num_good_genjets, parameters, dataset_name)

    #assign a numerical flag to each data event that corresponds to the data era
    assign_data_run_id(scalars, data_runs, dataset_era, is_mc, runmap_numerical)

    #Get the mask of events that pass trigger selection
    mask_events = select_events_trigger(scalars, parameters, mask_events, parameters["hlt_bits"][dataset_era])
    if not mask_vbf_filter is None:
        mask_events = mask_events & mask_vbf_filter

    #Event weight dictionary, 2 levels.
    #systematic name -> syst_dir -> individual weight value (not multiplied up) 
    weights_individual = {}
    weights_individual["nominal"] = {"nominal": NUMPY_LIB.ones(muons.numevents(), dtype=NUMPY_LIB.float32)}

    #Apply Rochester corrections to leading and subleading muon momenta
    if parameters["do_rochester_corrections"]:
        if debug:
            print("Before applying Rochester corrections: muons.pt={0:.2f} +- {1:.2f}".format(muons.pt.mean(), muons.pt.std()))
        do_rochester_corrections(
            is_mc,
            rochester_corrections[dataset_era],
            muons)
        if debug:
            print("After applying Rochester corrections muons.pt={0:.2f} +- {1:.2f}".format(muons.pt.mean(), muons.pt.std()))

    #get the two leading muons after applying all muon selection
    ret_mu = get_selected_muons(
        scalars,
        muons, trigobj, mask_events,
        parameters["muon_pt_leading"][dataset_era], parameters["muon_pt"],
        parameters["muon_eta"], parameters["muon_iso"],
        parameters["muon_id"][dataset_era], parameters["muon_trigger_match_dr"],
        parameters["muon_iso_trigger_matched"], parameters["muon_id_trigger_matched"][dataset_era]
    )
    print("muon selection eff", ret_mu["selected_muons"].sum() / float(muons.numobjects()))
    
    # Create arrays with just the leading and subleading particle contents for easier management
    mu_attrs = ["pt", "eta", "phi", "mass", "pdgId", "nTrackerLayers", "charge", "ptErr"]
    if is_mc:
        mu_attrs += ["genpt"]
    leading_muon = muons.select_nth(0, ret_mu["selected_events"], ret_mu["selected_muons"], attributes=mu_attrs)
    subleading_muon = muons.select_nth(1, ret_mu["selected_events"], ret_mu["selected_muons"], attributes=mu_attrs)
    if doverify:
        assert(NUMPY_LIB.all(leading_muon["pt"][leading_muon["pt"]>0] > parameters["muon_pt_leading"][dataset_era]))
        assert(NUMPY_LIB.all(subleading_muon["pt"][subleading_muon["pt"]>0] > parameters["muon_pt"]))

    if parameters["do_lepton_sf"] and is_mc:
        lepton_sf_values = compute_lepton_sf(leading_muon, subleading_muon,
            lepsf_iso[dataset_era], lepsf_id[dataset_era], lepsf_trig[dataset_era],
            use_cuda, dataset_era, NUMPY_LIB, debug)
        weights_individual["trigger"] = {
            "nominal": lepton_sf_values["trigger"],
            "up": lepton_sf_values["trigger__up"], 
            "down": lepton_sf_values["trigger__down"]
        }
        weights_individual["id"] = {
            "nominal": lepton_sf_values["id"],
            "up": lepton_sf_values["id__up"], 
            "down": lepton_sf_values["id__down"]
        }
        weights_individual["iso"] = {
            "nominal": lepton_sf_values["iso"],
            "up": lepton_sf_values["iso__up"], 
            "down": lepton_sf_values["iso__down"]
        }
        if doverify:
            for w in ["trigger", "id", "iso"]:
                m1 = weights_individual[w]["nominal"].mean()
                m2 = weights_individual[w]["up"].mean()
                m3 = weights_individual[w]["down"].mean()
                assert(m1 > m3 and m1 < m2) 
 
    #compute variated weights here to ensure the nominal weight contains all possible other weights  
    compute_event_weights(weights_individual, scalars, genweight_scalefactor, gghnnlopsw, LHEScalew, pu_corrections, is_mc, dataset_era, dataset_name)
 
    #actually multiply all the weights together with the appropriate up/down variations.
    #creates a 1-level dictionary with weights "nominal", "puweight__up", "puweight__down", ..." 
    weights_final = finalize_weights(weights_individual)

    #Just a check to verify that there are exactly 2 muons per event
    if doverify:
        z = ha.sum_in_offsets(
            muons,
            ret_mu["selected_muons"],
            ret_mu["selected_events"],
            ret_mu["selected_muons"],
            dtype=NUMPY_LIB.int8)
        assert(NUMPY_LIB.all(z[z!=0] == 2))

    # Get the selected electrons
    ret_el = get_selected_electrons(electrons, parameters["extra_electrons_pt"], parameters["extra_electrons_eta"], parameters["extra_electrons_id"])
    
    # Get the invariant mass of the dimuon system and compute mass windows
    higgs_inv_mass, higgs_pt = compute_inv_mass(muons, ret_mu["selected_events"], ret_mu["selected_muons"], use_cuda)
    higgs_inv_mass[NUMPY_LIB.isnan(higgs_inv_mass)] = -1
    higgs_inv_mass[NUMPY_LIB.isinf(higgs_inv_mass)] = -1
    higgs_inv_mass[higgs_inv_mass==0] = -1
    higgs_pt[NUMPY_LIB.isnan(higgs_pt)] = -1
    higgs_pt[NUMPY_LIB.isinf(higgs_pt)] = -1
    higgs_pt[higgs_pt==0] = -1
    
    fill_histograms_several(
        hists, "nominal", "hist__dimuon__",
        [
            (leading_muon["pt"], "leading_muon_pt", histo_bins["muon_pt"]),
            (subleading_muon["pt"], "subleading_muon_pt", histo_bins["muon_pt"]),
            (leading_muon["pt"], "leading_muon_eta", histo_bins["muon_eta"]),
            (subleading_muon["pt"], "subleading_muon_eta", histo_bins["muon_eta"]),
            (higgs_inv_mass, "inv_mass", histo_bins["inv_mass"]),
            (scalars["PV_npvsGood"], "npvs", histo_bins["npvs"]),
        ],
        ret_mu["selected_events"],
        weights_final,
        use_cuda
    )

    masswindow_z_peak = ((higgs_inv_mass >= parameters["masswindow_z_peak"][0]) & (higgs_inv_mass < parameters["masswindow_z_peak"][1]))
    masswindow_h_region = ((higgs_inv_mass >= parameters["masswindow_h_sideband"][0]) & (higgs_inv_mass < parameters["masswindow_h_sideband"][1]))
    masswindow_h_peak = ((higgs_inv_mass >= parameters["masswindow_h_peak"][0]) & (higgs_inv_mass < parameters["masswindow_h_peak"][1]))
    masswindow_h_sideband = masswindow_h_region & NUMPY_LIB.invert(masswindow_h_peak)

    #get the number of additional muons (not OS) that pass ID and iso cuts
    n_additional_muons = ha.sum_in_offsets(muons, ret_mu["additional_muon_sel"], ret_mu["selected_events"], ret_mu["additional_muon_sel"], dtype=NUMPY_LIB.int8)
    n_additional_electrons = ha.sum_in_offsets(electrons, ret_el["additional_electron_sel"], ret_mu["selected_events"], ret_el["additional_electron_sel"], dtype=NUMPY_LIB.int8)
    n_additional_leptons = n_additional_muons + n_additional_electrons

    #This computes the JEC, JER and associated systematics
    print("event selection eff based on 2 muons", ret_mu["selected_events"].sum() / float(len(mask_events)))

    #Do the jet ID selection and lepton cleaning just once for the nominal jet systematic
    #as that does not depend on jet pt
    selected_jets_id = get_selected_jets_id(
        jets, muons,
        parameters["jet_eta"],
        parameters["jet_mu_dr"],
        parameters["jet_id"],
        parameters["jet_puid"],
        parameters["jet_veto_eta"][0],
        parameters["jet_veto_eta"][1],
        parameters["jet_veto_raw_pt"],
        dataset_era)
    print("jet selection eff based on id", selected_jets_id.sum() / float(len(selected_jets_id)))

    jets_passing_id = jets.select_objects(selected_jets_id)
    
    print("Doing nominal jec on {0} jets".format(jets_passing_id.numobjects()))
    jet_systematics = JetTransformer(
        jets_passing_id, scalars,
        parameters,
        jetmet_corrections[dataset_era][parameters["jec_tag"][dataset_era]],
        NUMPY_LIB, use_cuda, is_mc)

    syst_to_consider = ["nominal"]
    if is_mc:
        syst_to_consider += ["Total"]
        if parameters["do_factorized_jec"]:
            syst_to_consider = syst_to_consider + jet_systematics.jet_uncertainty_names

    syst_to_consider = syst_to_consider
    print("entering jec loop with {0}".format(syst_to_consider))
    ret_jet_nominal = None
    
    #Now actually call the JEC computation for each scenario
    for uncertainty_name in syst_to_consider:
        #This will be the variated pt vector
        #print("computing variated pt for", uncertainty_name)
        var_up_down = jet_systematics.get_variated_pts(uncertainty_name)
        for jet_syst_name, jet_pt_vec in var_up_down.items():
            # For events where the JEC/JER was variated, fill only the nominal weight
            weights_selected = select_weights(weights_final, jet_syst_name)

            jet_pt_change = (jet_pt_vec - jets_passing_id.pt).mean()
            # Configure the jet pt vector to the variated one
            # Would need to also do the mass here
            jets_passing_id.pt = jet_pt_vec

            #Do the pt-dependent jet analysis now for all jets
            ret_jet = get_selected_jets(
                scalars,
                jets_passing_id,
                mask_events,
                parameters["jet_pt_subleading"][dataset_era],
                parameters["jet_btag"][dataset_era],
                is_mc, use_cuda
            )
            print("jet analysis syst={0} sdir={1} mean_pt_change={2:.4f} num_passing_jets={3} ".format(
                jet_syst_name[0], jet_syst_name[1], float(jet_pt_change), int(ret_jet["selected_jets"].sum()))
            )
            fill_histograms_several(
                hists, "nominal", "hist__dimuon__",
                [
                    (ret_jet["num_jets"], "num_jets" , histo_bins["numjets"]),
                ],
                ret_mu["selected_events"],
                weights_final,
                use_cuda
            )

            #print("jet selection eff based on ID & pt", ret_jet["selected_jets"].sum() / float(len(ret_jet["selected_jets"])))

            pt_balance = ret_jet["dijet_pt"] / higgs_pt

            # Set this default value as in Nan and Irene's code
            ret_jet["dijet_inv_mass"][ret_jet["num_jets"] < 2] = -1000.0
            # Get the data for the leading and subleading jets as contiguous vectors
            leading_jet = jets_passing_id.select_nth(
                0, ret_mu["selected_events"], ret_jet["selected_jets"],
                attributes=["pt", "eta", "phi", "mass", "qgl"])
            subleading_jet = jets_passing_id.select_nth(
                1, ret_mu["selected_events"], ret_jet["selected_jets"],
                attributes=["pt", "eta", "phi", "mass", "qgl"])

            if do_sync and jet_syst_name[0] == "nominal":
                sync_printout(ret_mu, muons, scalars,
                    leading_muon, subleading_muon, higgs_inv_mass,
                    n_additional_muons, n_additional_electrons,
                    ret_jet, leading_jet, subleading_jet)
           
            #compute DNN input variables in 2 muon, >=2jet region
            dnn_presel = (
                (ret_mu["selected_events"]) & (ret_jet["num_jets"] >= 2) &
                (leading_jet["pt"] > parameters["jet_pt_leading"][dataset_era])
            )

            #Histograms after dnn preselection
            fill_histograms_several(
                hists, jet_syst_name, "hist__dnn_presel__",
                [
                    (leading_jet["pt"], "leading_jet_pt", histo_bins["jet_pt"]),
                    (subleading_jet["pt"], "subleading_jet_pt", histo_bins["jet_pt"]),
                    (leading_jet["eta"], "leading_jet_eta", histo_bins["jet_eta"]),
                    (subleading_jet["eta"], "subleading_jet_eta", histo_bins["jet_eta"]),
                    (leading_jet["qgl"], "leading_jet_qgl", histo_bins["jet_qgl"]),
                    (subleading_jet["qgl"], "subleading_jet_qgl", histo_bins["jet_qgl"]),
                    (ret_jet["dijet_inv_mass"], "dijet_inv_mass", histo_bins["dijet_inv_mass"]),
                    (higgs_inv_mass, "inv_mass", histo_bins["inv_mass"]),
                    (scalars["SoftActivityJetNjets5"], "num_soft_jets", histo_bins["numjets"]),
                    (ret_jet["num_jets"], "num_jets" , histo_bins["numjets"]),
                    (pt_balance, "pt_balance", histo_bins["pt_balance"]),
                ],
                dnn_presel, 
                weights_selected,
                use_cuda
            )

            #Compute the DNN inputs, the DNN output, fill the DNN input and output variable histograms
            dnn_prediction = None
            dnn_vars, dnn_prediction, dnnPisa_prediction = compute_fill_dnn(hrelresolution,
               miscvariables, parameters, use_cuda, dnn_presel, dnn_model, dnn_normfactors, dnnPisa_model, dnnPisa_normfactors1, dnnPisa_normfactors2,
               scalars, leading_muon, subleading_muon, leading_jet, subleading_jet,
               ret_jet["num_jets"],ret_jet["num_jets_btag"],dataset_era
            )
            weights_in_dnn_presel = apply_mask(weights_selected, dnn_presel)
          
            if parameters["do_bdt_ucsd"]: 
                if not ((bdt_ucsd is None)):
                    bdt_pred = evaluate_bdt_ucsd(dnn_vars, bdt_ucsd)
                    dnn_vars["bdt_ucsd"] = NUMPY_LIB.array(bdt_pred, dtype=NUMPY_LIB.float32)
                #if not ((bdt2j_ucsd is None)):
                #    bdt2j_pred = evaluate_bdt2j_ucsd(dnn_vars, bdt2j_ucsd[dataset_era])
                #    dnn_vars["bdt2j_ucsd"] = bdt2j_pred
                #if not ((bdt01j_ucsd is None)):
                #    bdt01j_pred = evaluate_bdt01j_ucsd(dnn_vars, bdt01j_ucsd[dataset_era])
                #    dnn_vars["bdt01j_ucsd"] = bdt01j_pred

            #Assing a numerical category ID 
            category =  assign_category(
                ret_jet["num_jets"], ret_jet["num_jets_btag"],
                n_additional_muons, n_additional_electrons,
                ret_jet["dijet_inv_mass"],
                leading_jet, subleading_jet,
                parameters["cat5_dijet_inv_mass"],
                parameters["cat5_abs_jj_deta_cut"]
            )
            scalars["category"] = category


            #Assign the final analysis discriminator based on category
            #scalars["final_discriminator"] = NUMPY_LIB.zeros_like(higgs_inv_mass)
            if not (dnn_prediction is None):
                #inds_nonzero = NUMPY_LIB.nonzero(dnn_presel)[0]
                #if len(inds_nonzero) > 0:
                #    ha.copyto_dst_indices(scalars["final_discriminator"], dnn_prediction, inds_nonzero)
                #scalars["final_discriminator"][category != 5] = 0

                #Add some additional debugging info to the DNN training ntuples
                dnn_vars["cat_index"] = category[dnn_presel]
                dnn_vars["run"] = scalars["run"][dnn_presel]
                dnn_vars["lumi"] = scalars["luminosityBlock"][dnn_presel]
                dnn_vars["event"] = scalars["event"][dnn_presel]
                dnn_vars["dnn_pred"] = dnn_prediction
                dnn_vars["dnnPisa_pred"] = dnnPisa_prediction
                #Save the DNN training ntuples as npy files
                if parameters["save_dnn_vars"] and jet_syst_name[0] == "nominal" and parameter_set_name == "baseline":
                    dnn_vars_np = {k: NUMPY_LIB.asnumpy(v) for k, v in dnn_vars.items()}
                    if is_mc:
                        dnn_vars_np["nomweight"] = NUMPY_LIB.asnumpy(weights_in_dnn_presel["nominal"]*genweight_scalefactor)
                        dnn_vars_np["genweight"] = NUMPY_LIB.asnumpy(scalars["genWeight"][dnn_presel])
                        if "dy" in dataset_name or "ewk" in dataset_name:
                            for iScale in range(9):
                                dnn_vars_np["LHEScaleWeight__"+str(iScale)] = NUMPY_LIB.asnumpy(weights_in_dnn_presel["LHEScaleWeight__"+str(iScale)])
                    arrs = []
                    names = []
                    for k, v in dnn_vars_np.items():
                        arrs += [v]
                        names += [k]
                    arrdata = np.core.records.fromarrays(arrs, names=names)
                    outpath = "{0}/{1}".format(parameters["dnn_vars_path"], dataset_era) 
                    if not os.path.isdir(outpath):
                        os.makedirs(outpath)
                    np.save("{0}/{1}_{2}.npy".format(outpath, dataset_name, dataset_num_chunk), arrdata, allow_pickle=False)

            #Save histograms for numerical categories (cat5 only right now) and all mass bins
            for massbin_name, massbin_msk, mass_edges in [
                    ("h_peak", masswindow_h_peak, parameters["masswindow_h_peak"]),
                    ("h_sideband", masswindow_h_sideband, parameters["masswindow_h_sideband"]),
                    ("z_peak", masswindow_z_peak, parameters["masswindow_z_peak"])]:

                _sel0 = dnn_presel & massbin_msk

                for icat in [5, ]:
                    msk_cat = (category == icat)

                    hb = parameters["dnn_input_histogram_bins"]["dnn_pred"]

                    fill_histograms_several(
                        hists, jet_syst_name, "hist__dimuon_invmass_{0}_cat{1}__".format(massbin_name, icat),
                        [
                            (higgs_inv_mass, "inv_mass", histo_bins["inv_mass_{0}".format(massbin_name)]),
                            (leading_jet["pt"], "leading_jet_pt", histo_bins["jet_pt"]),
                            (subleading_jet["pt"], "subleading_jet_pt", histo_bins["jet_pt"]),
                            (leading_jet["eta"], "leading_jet_eta", histo_bins["jet_eta"]),
                            (subleading_jet["eta"], "subleading_jet_eta", histo_bins["jet_eta"]),
                            (ret_jet["dijet_inv_mass"], "dijet_inv_mass", histo_bins["dijet_inv_mass"]),
                            (scalars["SoftActivityJetNjets5"], "num_soft_jets", histo_bins["numjets"]),
                            (ret_jet["num_jets"], "num_jets" , histo_bins["numjets"]),
                            (pt_balance, "pt_balance", histo_bins["pt_balance"])
                        ],
                        (dnn_presel & massbin_msk & msk_cat),
                        weights_selected,
                        use_cuda
                    )

                    fill_histograms_several(
                        hists, jet_syst_name, "hist__dimuon_invmass_{0}_cat{1}__".format(massbin_name, icat),
                        [
                            (dnn_vars[varname], varname, histo_bins[varname])
                            for varname in dnn_vars.keys() if varname in histo_bins.keys()
                        ] + [
                            (dnn_vars["dnn_pred"], "dnn_pred2", histo_bins["dnn_pred2"][massbin_name])
                        ],
                        (dnn_presel & massbin_msk & msk_cat)[dnn_presel],
                        weights_in_dnn_presel,
                        use_cuda
                    )
        
         #end of isyst loop
    #end of uncertainty_name loop

    # Collect results
    ret = Results({
        "int_lumi": int_lumi,
    })

    for histname, r in hists.items():
        ret[histname] = Results(r)

    ret["numev_passed"] = get_numev_passed(
        muons.numevents(), {
        "trigger": mask_events,
        "muon": ret_mu["selected_events"]
    })

    if use_cuda:
        from numba import cuda
        cuda.synchronize()
 
    return ret

def fill_histograms_several(hists, systematic_name, histname_prefix, variables, mask, weights, use_cuda):
    all_arrays = []
    all_bins = []
    num_histograms = len(variables)

    for array, varname, bins in variables:
        if len(array) != len(variables[0][0]) or len(array) != len(mask) or len(array) != len(weights["nominal"]):
            raise Exception("Data array {0} is of incompatible size".format(varname))
        all_arrays += [array]
        all_bins += [bins]

    max_bins = max([b.shape[0] for b in all_bins])
    stacked_array = NUMPY_LIB.stack(all_arrays, axis=0)
    stacked_bins = np.concatenate(all_bins)
    nbins = np.array([len(b) for b in all_bins])
    nbins_sum = np.cumsum(nbins)
    nbins_sum = np.insert(nbins_sum, 0, [0])

    for weight_name, weight_array in weights.items():
        if use_cuda:
            nblocks = 32
            out_w = NUMPY_LIB.zeros((len(variables), nblocks, max_bins), dtype=NUMPY_LIB.float32)
            out_w2 = NUMPY_LIB.zeros((len(variables), nblocks, max_bins), dtype=NUMPY_LIB.float32)
            ha.fill_histogram_several[nblocks, 1024](
                stacked_array, weight_array, mask, stacked_bins,
                NUMPY_LIB.array(nbins), NUMPY_LIB.array(nbins_sum), out_w, out_w2
            )
            cuda.synchronize()

            out_w = out_w.sum(axis=1)
            out_w2 = out_w2.sum(axis=1)

            out_w = NUMPY_LIB.asnumpy(out_w)
            out_w2 = NUMPY_LIB.asnumpy(out_w2)
        else:
            out_w = NUMPY_LIB.zeros((len(variables), max_bins), dtype=NUMPY_LIB.float32)
            out_w2 = NUMPY_LIB.zeros((len(variables), max_bins), dtype=NUMPY_LIB.float32)
            ha.fill_histogram_several(
                stacked_array, weight_array, mask, stacked_bins,
                nbins, nbins_sum, out_w, out_w2
            )

        out_w_separated = [out_w[i, 0:nbins[i]-1] for i in range(num_histograms)]
        out_w2_separated = [out_w2[i, 0:nbins[i]-1] for i in range(num_histograms)]

        for ihist in range(num_histograms):
            hist_name = histname_prefix + variables[ihist][1]
            bins = variables[ihist][2]
            target_histogram = Histogram(out_w_separated[ihist], out_w2_separated[ihist], bins)
            target = {weight_name: target_histogram}
            update_histograms_systematic(hists, hist_name, systematic_name, target)
    
def compute_integrated_luminosity(scalars, lumimask, lumidata, dataset_era, mask_events, is_mc):
    int_lumi = 0
    if not is_mc:
        runs = NUMPY_LIB.asnumpy(scalars["run"])
        lumis = NUMPY_LIB.asnumpy(scalars["luminosityBlock"])
        if not (lumimask is None):
           #keep events passing golden JSON
           mask_lumi_golden_json = lumimask[dataset_era](runs, lumis)
           lumi_eff = mask_lumi_golden_json.sum()/len(mask_lumi_golden_json)
           if not (lumi_eff > 0.5):
               print("WARNING, data file had low lumi efficiency", lumi_eff)  
           mask_events = mask_events & NUMPY_LIB.array(mask_lumi_golden_json) 
           #get integrated luminosity in this file
           if not (lumidata is None):
               int_lumi = get_int_lumi(runs, lumis, mask_lumi_golden_json, lumidata[dataset_era])
    return int_lumi

def get_genparticles(data, muons, jets, is_mc, use_cuda):
    genJet = None
    genpart = None

    if is_mc:
        genJet = data["GenJet"]
        genpart = data["GenPart"]
        muons_genpt = NUMPY_LIB.zeros(muons.numobjects(), dtype=NUMPY_LIB.float32)
        jets_genpt = NUMPY_LIB.zeros(jets.numobjects(), dtype=NUMPY_LIB.float32)
        jets_genmass = NUMPY_LIB.zeros(jets.numobjects(), dtype=NUMPY_LIB.float32)
        if not use_cuda:
            get_genpt_cpu(muons.offsets, muons.genPartIdx, genpart.offsets, genpart.pt, muons_genpt)
            get_genpt_cpu(jets.offsets, jets.genJetIdx, genJet.offsets, genJet.pt, jets_genpt)
            get_genpt_cpu(jets.offsets, jets.genJetIdx, genJet.offsets, genJet.mass, jets_genmass)
        else:
            get_genpt_cuda[32,1024](muons.offsets, muons.genPartIdx, genpart.offsets, genpart.pt, muons_genpt)
            get_genpt_cuda[32,1024](jets.offsets, jets.genJetIdx, genJet.offsets, genJet.pt, jets_genpt)
            get_genpt_cuda[32,1024](jets.offsets, jets.genJetIdx, genJet.offsets, genJet.mass, jets_genmass)
        muons.attrs_data["genpt"] = muons_genpt
        jets.attrs_data["genpt"] = jets_genpt
        jets.attrs_data["genmass"] = jets_genmass
    return genJet, genpart

def assign_data_run_id(scalars, data_runs, dataset_era, is_mc, runmap_numerical):
    if not is_mc:
        scalars["run_index"] = NUMPY_LIB.zeros_like(scalars["run"])
        scalars["run_index"][:] = -1
        runranges_list = data_runs[dataset_era]
        for run_start, run_end, run_name in runranges_list:
            msk = (scalars["run"] >= run_start) & (scalars["run"] <= run_end)
            scalars["run_index"][msk] = runmap_numerical[run_name]
        assert(NUMPY_LIB.sum(scalars["run_index"]==-1)==0)

def finalize_weights(weights, all_weight_names=None):
    if all_weight_names is None:
        all_weight_names = weights.keys()
    
    ret = {}
    ret["nominal"] = NUMPY_LIB.copy(weights["nominal"]["nominal"])

    #multitply up all the nominal weights
    for this_syst in all_weight_names:
        if this_syst == "nominal" or this_syst == "LHEScaleWeight":
            continue
        ret["nominal"] *= weights[this_syst]["nominal"]

    #create the variated weights, where just one weight is variated up or down
    for this_syst in all_weight_names:
        if this_syst == "nominal":
            continue
        elif this_syst == "LHEScaleWeight":
            for sdir in ["0", "1", "2", "3", "4", "5", "6", "7", "8"]:
                wval_this_systematic = weights[this_syst][sdir]
                wtot = NUMPY_LIB.copy(ret["nominal"])
                wtot *= wval_this_systematic
                ret["{0}__{1}".format(this_syst, sdir)] = wtot
        else:
            for sdir in ["up", "down", "off"]:
                #for the particular weight or scenario we are considering, get the variated value
                if sdir == "off":
                    wval_this_systematic = NUMPY_LIB.ones_like(ret["nominal"])
                else:
                    wval_this_systematic = weights[this_syst][sdir]

                #for other weights, get the nominal
                wtot = NUMPY_LIB.copy(weights["nominal"]["nominal"])

                wtot *= wval_this_systematic

                for other_syst in all_weight_names:
                    if (other_syst == this_syst or other_syst == "nominal") or other_syst == "LHEScaleWeight":
                        continue
                    wtot *= weights[other_syst]["nominal"] 
                ret["{0}__{1}".format(this_syst, sdir)] = wtot
    
    for k in ret.keys():
        print("finalized weight", k, ret[k].mean())
    return ret

def compute_event_weights(weights, scalars, genweight_scalefactor, gghw, LHEScalew, pu_corrections, is_mc, dataset_era, dataset_name):
    if is_mc:
        if "ggh" in dataset_name:
            weights["nominal"]["nominal"] = scalars["genWeight"] * genweight_scalefactor * gghw
        else:
            weights["nominal"]["nominal"] = scalars["genWeight"] * genweight_scalefactor
 
        if debug:
            print("mean genWeight=", scalars["genWeight"].mean())
            print("sum genWeight=", scalars["genWeight"].sum())
        pu_weights, pu_weights_up, pu_weights_down = compute_pu_weights(
            pu_corrections[dataset_era],
            weights["nominal"]["nominal"],
            scalars["Pileup_nTrueInt"],
            scalars["PV_npvsGood"])

        if debug:
            print("pu_weights", pu_weights.mean(), pu_weights.std())
            print("pu_weights_up", pu_weights_up.mean(), pu_weights_up.std())
            print("pu_weights_down", pu_weights_down.mean(), pu_weights_down.std())
        
        weights["puWeight"] = {"nominal": pu_weights, "up": pu_weights_up, "down": pu_weights_down}
        
        weights["L1PreFiringWeight"] = {
            "nominal": NUMPY_LIB.ones_like(weights["nominal"]["nominal"]),
            "up": NUMPY_LIB.ones_like(weights["nominal"]["nominal"]),
            "down": NUMPY_LIB.ones_like(weights["nominal"]["nominal"]), 
        }
        if dataset_era == "2016" or dataset_era == "2017":
            if debug:
                print("mean L1PreFiringWeight_Nom=", scalars["L1PreFiringWeight_Nom"].mean())
                print("mean L1PreFiringWeight_Up=", scalars["L1PreFiringWeight_Up"].mean())
                print("mean L1PreFiringWeight_Dn=", scalars["L1PreFiringWeight_Dn"].mean())
            weights["L1PreFiringWeight"] = {
                "nominal": scalars["L1PreFiringWeight_Nom"],
                "up": scalars["L1PreFiringWeight_Up"],
                "down": scalars["L1PreFiringWeight_Dn"]}

        #hardcode the number of LHE weights
        n_max_lheweights = 9
        weights["LHEScaleWeight"] = {
            str(n): NUMPY_LIB.ones_like(weights["nominal"]["nominal"])
            for n in range(n_max_lheweights)}

        #only defined for dy and ewk samples
        if ("dy" in dataset_name) or ("ewk" in dataset_name):
            nevt = len(weights["nominal"]["nominal"])
            for iScale in range(n_max_lheweights):
                LHEScalew_all = NUMPY_LIB.zeros(nevt, dtype=NUMPY_LIB.float32);
                get_theoryweights_cpu(LHEScalew.offsets, LHEScalew.LHEScaleWeight, iScale, LHEScalew_all)
                weights["LHEScaleWeight"][str(iScale)] = LHEScalew_all

def evaluate_bdt_ucsd(dnn_vars, gbr_bdt):
    # BDT var=hmmpt
    # BDT var=hmmrap
    # BDT var=hmmthetacs
    # BDT var=hmmphics
    # BDT var=j1pt
    # BDT var=j1eta
    # BDT var=j2pt
    # BDT var=detajj
    # BDT var=dphijj
    # BDT var=mjj
    # BDT var=met
    # BDT var=zepen
    # BDT var=hmass
    # BDT var=njets
    # BDT var=drmj
    varnames = [
        "Higgs_pt",
        "Higgs_rapidity",
        "hmmthetacs",
        "hmmphics",
        "leadingJet_pt",
        "leadingJet_eta",
        "subleadingJet_pt",
        "dEta_jj_abs",
        "dPhi_jj_mod_abs",
        "M_jj",
        "MET_pt",
        "Zep_rapidity",
        "Higgs_mass",
        "num_jets",
        "dRmin_mj",
    ]

    X = NUMPY_LIB.asnumpy(NUMPY_LIB.stack([dnn_vars[vname] for vname in varnames], axis=1))
    #print("bdt_ucsd inputs")
    #print(X.mean(axis=0), X.min(axis=0), X.max(axis=0), sep="\n")
    y = gbr_bdt.compute(X)
    if X.shape[0] > 0:
        for ivar in range(len(varnames)):
            print(varnames[ivar], X[:, ivar].min(), X[:, ivar].max())
        print("bdt_ucsd eval", y.mean(), y.std(), y.min(), y.max())
    return y

def evaluate_bdt2j_ucsd(dnn_vars, gbr_bdt):
    varnames = [
        "Higgs_pt",
        "Higgs_rapidity",
        "hmmthetacs",
        "hmmphics",
        "leadingJet_pt",
        "leadingJet_eta",
        "subleadingJet_pt",
        "dEta_jj_abs",
        "dPhi_jj_mod_abs",
        "M_jj",
        "MET_pt",
        "Zep_rapidity",
        "num_jets",
        "dRmin_mj",
        "m1ptOverMass",
        "m2ptOverMass",
        "m1eta",
        "m2eta",
    ]

    X = NUMPY_LIB.asnumpy(NUMPY_LIB.stack([dnn_vars[vname] for vname in varnames], axis=1))
    #print("bdt_ucsd inputs")
    #print(X.mean(axis=0), X.min(axis=0), X.max(axis=0), sep="\n")
    y = gbr_bdt.compute(X)
    if X.shape[0] > 0:
        for ivar in range(len(varnames)):
            print(varnames[ivar], X[:, ivar].min(), X[:, ivar].max())
        print("bdt_ucsd2j eval", y.mean(), y.std(), y.min(), y.max())
    return y

def evaluate_bdt01j_ucsd(dnn_vars, gbr_bdt):
    varnames = [
        "Higgs_pt",
        "Higgs_rapidity",
        "hmmthetacs",
        "hmmphics",
        "leadingJet_pt",
        "leadingJet_eta",
        "MET_pt",
        "num_jets_btag",
        "dRmin_mj",
        "num_jets",
        "m1ptOverMass",
        "m2ptOverMass",
        "m1eta",
        "m2eta",
    ]

    X = NUMPY_LIB.asnumpy(NUMPY_LIB.stack([dnn_vars[vname] for vname in varnames], axis=1))
    #print("bdt_ucsd inputs")
    #print(X.mean(axis=0), X.min(axis=0), X.max(axis=0), sep="\n")
    y = gbr_bdt.compute(X)
    if X.shape[0] > 0:
        for ivar in range(len(varnames)):
            print(varnames[ivar], X[:, ivar].min(), X[:, ivar].max())
        print("bdt_ucsd01j eval", y.mean(), y.std(), y.min(), y.max())
    return y

def vbf_genfilter(genjet_inv_mass, num_good_genjets, parameters, dataset_name):
    mask_dijet_genmass = (genjet_inv_mass > parameters["vbf_filter_mjj_cut"])
    mask_2gj = num_good_genjets >= 2
    invert_mask = parameters["vbf_filter"][dataset_name]
    if invert_mask:
        mask_dijet_genmass = NUMPY_LIB.invert(mask_dijet_genmass)

    mask_out = NUMPY_LIB.ones_like(mask_dijet_genmass)
    mask_out[mask_2gj & NUMPY_LIB.invert(mask_dijet_genmass)] = False
    print("VBF genfilter on sample", dataset_name,
        "numev", len(mask_out), "2gj", mask_2gj.sum(),
        "2gj&&mjj", (mask_2gj&mask_dijet_genmass).sum(), "out", mask_out.sum()
    )
 
    return mask_out

def run_cache(
    cmdline_args,
    outpath,
    job_descriptions,
    parameters):

    print("run_cache with job_descriptions=", len(job_descriptions))
    nev_total = 0
    nev_loaded = 0
    t0 = time.time()
            
    processed_size_mb = 0

    _nev_total, _processed_size_mb = cache_data(
        job_descriptions,
        parameters,
        cmdline_args)

    nev_total += _nev_total
    processed_size_mb += _processed_size_mb

    t1 = time.time()
    dt = t1 - t0
    print("In run_cache, processed {nev:.2E} events in total {size:.2f} GB, {dt:.1f} seconds, {evspeed:.2E} Hz, {sizespeed:.2f} MB/s".format(
        nev=nev_total, dt=dt, size=processed_size_mb/1024.0, evspeed=nev_total/dt, sizespeed=processed_size_mb/dt)
    )

    bench_ret = {}
    bench_ret.update(cmdline_args.__dict__)
    bench_ret["hostname"] = os.uname()[1]
    bench_ret["nev_total"] = nev_total
    bench_ret["total_time"] = dt
    bench_ret["evspeed"] = nev_total/dt/1000/1000
    with open(cmdline_args.out + "/analysis_benchmarks.txt", "a") as of:
        of.write(json.dumps(bench_ret) + '\n')

#Main analysis entry point
def run_analysis(
    cmdline_args,
    outpath,
    job_descriptions,
    parameters,
    analysis_corrections):

    #Keep track of number of events
    nev_total = 0
    nev_loaded = 0
    t0 = time.time()
            
    processed_size_mb = 0

    #Create a thread that will load data in the background
    training_set_generator = InputGen(
        job_descriptions,
        cmdline_args.cache_location,
        cmdline_args.datapath)

    threadk = thread_killer()
    threadk.set_tokill(False)
    train_batches_queue = Queue(maxsize=10)
    
    #Start the thread if using a multithreaded approach
    if cmdline_args.async_data:
        input_thread = Thread(target=threaded_batches_feeder, args=(threadk, train_batches_queue, training_set_generator))
        input_thread.start()

    # metrics_thread = Thread(target=threaded_metrics, args=(threadk, train_batches_queue))
    # metrics_thread.start()

    rets = []
    num_processed = 0
   
    cache_metadata = []

    tprev = time.time()
    #loop over all data, call the analyze function
    while num_processed < len(training_set_generator):

        # In case we are processing data synchronously, just load the dataset here
        # and put to queue.
        if not cmdline_args.async_data:
            ds = training_set_generator.nextone()
            if ds is None:
                break
            train_batches_queue.put(ds)

        # #Progress indicator for each chunk of files
        # sys.stdout.write(".");sys.stdout.flush()

        #Process the dataset
        ret, ds, nev, memsize = event_loop(
            train_batches_queue,
            cmdline_args.use_cuda,
            verbose=False,
            lumimask=analysis_corrections.lumimask,
            lumidata=analysis_corrections.lumidata,
            pu_corrections=analysis_corrections.pu_corrections,
            rochester_corrections=analysis_corrections.rochester_corrections,
            lepsf_iso=analysis_corrections.lepsf_iso,
            lepsf_id=analysis_corrections.lepsf_id,
            lepsf_trig=analysis_corrections.lepsf_trig,
            parameters=parameters,
            dnn_model=analysis_corrections.dnn_model,
            dnn_normfactors=analysis_corrections.dnn_normfactors,
            dnnPisa_model=analysis_corrections.dnnPisa_model,
            dnnPisa_normfactors1=analysis_corrections.dnnPisa_normfactors1,
            dnnPisa_normfactors2=analysis_corrections.dnnPisa_normfactors2,
            jetmet_corrections=analysis_corrections.jetmet_corrections,
            do_sync = cmdline_args.do_sync,
            bdt_ucsd  = analysis_corrections.bdt_ucsd,
            bdt2j_ucsd  = analysis_corrections.bdt2j_ucsd,
            bdt01j_ucsd  = analysis_corrections.bdt01j_ucsd,
            miscvariables = analysis_corrections.miscvariables,
            nnlopsreweighting = analysis_corrections.nnlopsreweighting,
            hrelresolution = analysis_corrections.hrelresolution)

        tnext = time.time()
        print("processed {0:.2E} ev/s".format(nev/float(tnext-tprev)))
        sys.stdout.flush()
        tprev = tnext

        with open("{0}/{1}_{2}_{3}.pkl".format(outpath, ds.name, ds.era, ds.num_chunk), "wb") as fi:
            pickle.dump(ret, fi, protocol=pickle.HIGHEST_PROTOCOL)

        processed_size_mb += memsize
        nev_total += sum([md["numevents"] for md in ret["cache_metadata"]])
        nev_loaded += nev
        num_processed += 1
    print()

    #clean up threads
    threadk.set_tokill(True)
    #metrics_thread.join() 

    # #save output
    # ret = sum(rets, Results({}))
    # assert(ret["baseline"]["int_lumi"] == sum([r["baseline"]["int_lumi"] for r in rets]))
    # print(ret["baseline"]["int_lumi"])
    # if is_mc:
    #     ret["genEventSumw"] = genweight_scalefactor * sum([md["precomputed_results"]["genEventSumw"] for md in ret["cache_metadata"]])
    #     ret["genEventSumw2"] = genweight_scalefactor * sum([md["precomputed_results"]["genEventSumw2"] for md in ret["cache_metadata"]])
    #     print(dataset_name, "sum genweights", ret["genEventSumw"])
    # ret.save_json("{0}/{1}_{2}.json".format(outpath, dataset_name, dataset_era))
    
    t1 = time.time()
    dt = t1 - t0
    print("In run_analysis, processed {nev_loaded:.2E} ({nev:.2E} raw NanoAOD equivalent) events in total {size:.2f} GB, {dt:.1f} seconds, {evspeed:.2E} Hz, {sizespeed:.2f} MB/s".format(
        nev=nev_total, nev_loaded=nev_loaded, dt=dt,
        size=processed_size_mb/1024.0, evspeed=nev_total/dt, sizespeed=processed_size_mb/dt,
        )
    )
   
    if len(global_metrics) > 0:
        metrics_results = {}
        for k in global_metrics[0]: 
            metrics_results[k] = []
        for gm in global_metrics:
            for k in gm.keys():
                metrics_results[k] += [gm[k]]
        for k in metrics_results.keys():
            print("metric {0} avg={1:.2f} max={2:.2f}".format(k, np.mean(metrics_results[k]), np.max(metrics_results[k])))

    bench_ret = {}
    bench_ret.update(cmdline_args.__dict__)
    bench_ret["hostname"] = os.uname()[1]
    bench_ret["nev_total"] = nev_total
    bench_ret["total_time"] = dt
    bench_ret["evspeed"] = nev_total/dt/1000/1000
    with open(cmdline_args.out + "/analysis_benchmarks.txt", "a") as of:
        of.write(json.dumps(bench_ret) + '\n')

def event_loop(train_batches_queue, use_cuda, **kwargs):
    ds = train_batches_queue.get(block=True)
    #print("event_loop nev={0}, queued={1}".format(len(ds), train_batches_queue.qsize()))

    #copy dataset to GPU and make sure future operations are done on it
    if use_cuda:
        import cupy
        ds.numpy_lib = cupy
        ds.move_to_device(cupy)

    parameters = kwargs.pop("parameters")

    ret = {}
    for parameter_set_name, parameter_set in parameters.items():
        print("doing analysis on parameter set", parameter_set_name)
        ret[parameter_set_name] = ds.analyze(
            analyze_data,
            use_cuda = use_cuda,
            parameter_set_name = parameter_set_name,
            parameters = parameter_set,
            dataset_era = ds.era,
            dataset_name = ds.name,
            dataset_num_chunk = ds.num_chunk,
            is_mc = ds.is_mc,
            **kwargs)
    ret["num_events"] = len(ds)

    train_batches_queue.task_done()

    #clean up CUDA memory
    if use_cuda:
        mempool = cupy.get_default_memory_pool()
        pinned_mempool = cupy.get_default_pinned_memory_pool()
        mempool.free_all_blocks()
        pinned_mempool.free_all_blocks()
     
    ret["cache_metadata"] = ds.cache_metadata
    if ds.is_mc:
        ret["genEventSumw"] = genweight_scalefactor * sum([
            md["precomputed_results"]["genEventSumw"] for md in ret["cache_metadata"]
        ])
    ret = Results(ret)
    return ret, ds, len(ds), ds.memsize()/1024.0/1024.0

def get_histogram(data, weights, bins, mask=None):
    """Given N-unit vectors of data and weights, returns the histogram in bins
    """
    return Histogram(*ha.histogram_from_vector(data, weights, bins, mask))

def get_selected_muons(
    scalars,
    muons, trigobj, mask_events,
    mu_pt_cut_leading, mu_pt_cut_subleading,
    mu_aeta_cut, mu_iso_cut, muon_id_type,
    muon_trig_match_dr, mu_iso_trig_matched_cut, muon_id_trig_matched_type):
    """
    Given a list of muons in events, selects the muons that pass quality, momentum and charge criteria.
    Selects events that have at least 2 such muons. Selections are made by producing boolean masks.

    muons (JaggedStruct) - The muon content of a given file
    trigobj (JaggedStruct) - The trigger objects
    mask_events (array of bool) - a mask of events that are used for muon processing
    mu_pt_cut_leading (float) - the pt cut on the leading muon
    mu_pt_cut_subleading (float) - the pt cut on all other muons
    mu_aeta_cut (float) - upper abs eta cut signal on muon
    mu_iso_cut (float) - cut to choose isolated muons
    muon_id_type (string) - "medium" or "tight" for the muon ID
    muon_trig_match_dr (float) - dR matching criterion between trigger object and leading muon
    mu_iso_trig_matched_cut (float) - tight isolation requirement the trigger matched muon
    muon_id_trig_matched_type (string) - "tight" muon ID requirement for trigger matched muon
    """
    passes_iso = muons.pfRelIso04_all < mu_iso_cut
    passes_iso_trig_matched = muons.pfRelIso04_all < mu_iso_trig_matched_cut

    if muon_id_type == "medium":
        passes_id = muons.mediumId == 1
    elif muon_id_type == "tight":
        passes_id = muons.tightId == 1
    else:
        raise Exception("unknown muon id: {0}".format(muon_id_type))

    if muon_id_trig_matched_type == "tight":
        passes_id_trig_matched = muons.tightId == 1
    else:
        raise Exception("unknown muon id: {0}".format(muon_id_type))

    #find muons that pass ID
    passes_global = (muons.isGlobal == 1)
    passes_subleading_pt = muons.pt > mu_pt_cut_subleading
    passes_leading_pt = muons.pt > mu_pt_cut_leading
    passes_aeta = NUMPY_LIB.abs(muons.eta) < mu_aeta_cut
    muons_passing_id =  (
        passes_global & passes_iso & passes_id &
        passes_subleading_pt & passes_aeta
    )

    muons_passing_id_trig_matched =  (
        passes_global & passes_iso_trig_matched & passes_id_trig_matched &
        passes_subleading_pt & passes_aeta
    )

    #Get muons that are high-pt and are matched to trigger object
    mask_trigger_objects_mu = (trigobj.id == 13)
    muons_matched_to_trigobj = NUMPY_LIB.invert(ha.mask_deltar_first(
        muons, muons_passing_id_trig_matched & passes_leading_pt, trigobj,
        mask_trigger_objects_mu, muon_trig_match_dr
    ))
    muons.attrs_data["triggermatch"] = muons_matched_to_trigobj
    muons.attrs_data["pass_id"] = muons_passing_id
    muons.attrs_data["passes_leading_pt"] = passes_leading_pt

    #At least one muon must be matched to trigger object, find such events
    events_passes_triggermatch = ha.sum_in_offsets(
        muons, muons_matched_to_trigobj, mask_events,
        muons.masks["all"], NUMPY_LIB.int8
    ) >= 1

    #select events that have muons passing cuts: 2 passing ID, 1 passing leading pt, 2 passing subleading pt
    events_passes_muid = ha.sum_in_offsets(
        muons, muons_passing_id, mask_events, muons.masks["all"],
        NUMPY_LIB.int8) >= 2
    events_passes_leading_pt = ha.sum_in_offsets(
        muons, muons_passing_id & passes_leading_pt, mask_events,
        muons.masks["all"], NUMPY_LIB.int8) >= 1
    events_passes_subleading_pt = ha.sum_in_offsets(
        muons, muons_passing_id & passes_subleading_pt,
        mask_events, muons.masks["all"], NUMPY_LIB.int8) >= 2

    #Get the mask of selected events
    base_event_sel = (
        mask_events &
        events_passes_triggermatch &
        events_passes_muid &
        events_passes_leading_pt &
        events_passes_subleading_pt
    )

    #Find two opposite sign muons among the muons passing ID and subleading pt
    muons_passing_os = ha.select_muons_opposite_sign(
        muons, muons_passing_id & passes_subleading_pt)
    events_passes_os = ha.sum_in_offsets(
        muons, muons_passing_os, mask_events,
        muons.masks["all"], NUMPY_LIB.int32) == 2

    muons.attrs_data["pass_os"] = muons_passing_os
    final_event_sel = base_event_sel & events_passes_os
    final_muon_sel = muons_passing_id & passes_subleading_pt & muons_passing_os
    additional_muon_sel = muons_passing_id & NUMPY_LIB.invert(muons_passing_os)
    muons.masks["iso_id_aeta"] = passes_iso & passes_id & passes_aeta

    if debug:
        for evtid in debug_event_ids:
            idx = np.where(scalars["event"] == evtid)[0][0]
            print("muons")
            jaggedstruct_print(muons, idx,
                ["pt", "eta", "phi", "charge", "pfRelIso04_all", "mediumId",
                "isGlobal", "isTracker", 
                "triggermatch", "pass_id", "passes_leading_pt"])

    return {
        "selected_events": final_event_sel,
        "muons_passing_id_pt": muons_passing_id & passes_subleading_pt,
        "selected_muons": final_muon_sel,
        "muons_passing_os": muons_passing_os,
        "additional_muon_sel": additional_muon_sel,
    }

def get_bit_values(array, bit_index):
    """
    Given an array of N binary values (e.g. jet IDs), return the bit value at bit_index in [0, N-1].
    """
    return (array & 2**(bit_index)) >> 1

def get_selected_jets_id(
    jets,
    muons,
    jet_eta_cut,
    jet_dr_cut,
    jet_id,
    jet_puid,
    jet_veto_eta_lower_cut,
    jet_veto_eta_upper_cut,
    jet_veto_raw_pt,
    dataset_era):

    #2017 and 2018: jetId = Var("userInt('tightId')*2+4*userInt('tightIdLepVeto'))
    #Jet ID flags bit0 is loose (always false in 2017 since it does not exist), bit1 is tight, bit2 is tightLepVeto
    #run2_nanoAOD_94X2016: jetId = Var("userInt('tightIdLepVeto')*4+userInt('tightId')*2+userInt('looseId')",int,doc="Jet ID flags bit1 is loose, bit2 is tight, bit3 is tightLepVeto"
    if jet_id == "tight":
        if dataset_era == "2017" or dataset_era == "2018":
            pass_jetid = jets.jetId >= 2
        else:
            pass_jetid = jets.jetId >= 3
    elif jet_id == "loose": 
        pass_jetid = jets.jetId >= 1

    #The value is a bit representation of the fulfilled working points: tight (1), medium (2), and loose (4).
    #As tight is also medium and medium is also loose, there are only 4 different settings: 0 (no WP, 0b000), 4 (loose, 0b100), 6 (medium, 0b110), and 7 (tight, 0b111).
    if jet_puid == "loose":
        pass_jet_puid = jets.puId >= 4
    elif jet_puid == "medium":
        pass_jet_puid = jets.puId >= 6
    elif jet_puid == "tight":
        pass_jet_puid = jets.puId >= 7
    elif jet_puid == "none":
        pass_jet_puid = NUMPY_LIB.ones(jets.numobjects(), dtype=NUMPY_LIB.bool)

    pass_qgl = jets.qgl > -1 

    abs_eta = NUMPY_LIB.abs(jets.eta)
    raw_pt = compute_jet_raw_pt(jets)
    selected_jets = (
    	(abs_eta < jet_eta_cut) &
            pass_jetid & pass_jet_puid & pass_qgl
    )
    if dataset_era == "2017":
        jet_eta_pass_veto = NUMPY_LIB.logical_or(
            (raw_pt > jet_veto_raw_pt),
            NUMPY_LIB.logical_or(
                (abs_eta > jet_veto_eta_upper_cut),
                (abs_eta < jet_veto_eta_lower_cut)
            )
        )
        selected_jets = selected_jets & jet_eta_pass_veto
    
    jets_pass_dr = ha.mask_deltar_first(
        jets, selected_jets, muons,
        muons.masks["iso_id_aeta"], jet_dr_cut)

    jets.masks["pass_dr"] = jets_pass_dr

    selected_jets = selected_jets & jets_pass_dr
    return selected_jets

def get_selected_jets(
    scalars,
    jets,
    mask_events,
    jet_pt_cut_subleading,
    jet_btag,
    is_mc,
    use_cuda
    ):
    """
    Given jets and selected muons in events, choose jets that pass quality
    criteria and that are not dR-matched to muons.
    """

    selected_jets = (jets.pt > jet_pt_cut_subleading)
   
    #produce a mask that selects the first two selected jets 
    first_two_jets = NUMPY_LIB.zeros_like(selected_jets)
   
    inds = NUMPY_LIB.zeros_like(mask_events, dtype=NUMPY_LIB.int32) 
    targets = NUMPY_LIB.ones_like(mask_events, dtype=NUMPY_LIB.int32) 
    inds[:] = 0
    ha.set_in_offsets(first_two_jets, jets.offsets, inds, targets, mask_events, selected_jets)
    inds[:] = 1
    ha.set_in_offsets(first_two_jets, jets.offsets, inds, targets, mask_events, selected_jets)
    jets.attrs_data["selected"] = selected_jets
    jets.attrs_data["first_two"] = first_two_jets

    dijet_inv_mass, dijet_pt = compute_inv_mass(jets, mask_events, selected_jets & first_two_jets, use_cuda)
    
    selected_jets_btag = selected_jets & (jets.btagDeepB >= jet_btag)

    num_jets = ha.sum_in_offsets(jets, selected_jets, mask_events,
        jets.masks["all"], NUMPY_LIB.int8)

    num_jets_btag = ha.sum_in_offsets(jets, selected_jets_btag, mask_events,
        jets.masks["all"], NUMPY_LIB.int8)

    # if debug:
    #     for evtid in debug_event_ids:
    #         idx = np.where(scalars["event"] == evtid)[0][0]
    #         print("jets")
    #         jaggedstruct_print(jets, idx,
    #             ["pt", "eta", "phi", "mass", "jetId", "puId",
    #             "pass_dr", "selected", 
    #             "first_two"])

    ret = {
        "selected_jets": selected_jets,
        "num_jets": num_jets,
        "num_jets_btag": num_jets_btag,
        "dijet_inv_mass": dijet_inv_mass,
        "dijet_pt": dijet_pt
    }

    return ret

def get_selected_electrons(electrons, pt_cut, eta_cut, id_type):
    if id_type == "mvaFall17V1Iso_WP90":
        passes_id = electrons.mvaFall17V1Iso_WP90 == 1
    elif id_type == "none":
        passes_id = NUMPY_LIB.ones(electrons.num_objects, dtype=NUMPY_LIB.bool)
    else:
        raise Exception("Unknown id_type {0}".format(id_type))
        
    passes_pt = electrons.pt > pt_cut
    passes_eta = NUMPY_LIB.abs(electrons.eta) < eta_cut
    final_electron_sel = passes_id & passes_pt & passes_eta

    return {
        "additional_electron_sel": final_electron_sel,
    }

def compute_jet_raw_pt(jets):
    """
    Computs the raw pt of a jet.
    """
    raw_pt = jets.pt * (1.0 - jets.rawFactor)
    return raw_pt

def compute_inv_mass(objects, mask_events, mask_objects, use_cuda):
    inv_mass = NUMPY_LIB.zeros(len(mask_events), dtype=np.float32)
    pt_total = NUMPY_LIB.zeros(len(mask_events), dtype=np.float32)
    if use_cuda:
        compute_inv_mass_cudakernel[32, 1024](
            objects.offsets, objects.pt, objects.eta, objects.phi, objects.mass,
            mask_events, mask_objects, inv_mass, pt_total)
        cuda.synchronize()
    else:
        compute_inv_mass_kernel(objects.offsets,
            objects.pt, objects.eta, objects.phi, objects.mass,
            mask_events, mask_objects, inv_mass, pt_total)
    return inv_mass, pt_total

@numba.njit(parallel=True, fastmath=True)
def compute_inv_mass_kernel(offsets, pts, etas, phis, masses, mask_events, mask_objects, out_inv_mass, out_pt_total):
    for iev in numba.prange(offsets.shape[0]-1):
        if mask_events[iev]:
            start = np.uint64(offsets[iev])
            end = np.uint64(offsets[iev + 1])
            
            px_total = np.float32(0.0)
            py_total = np.float32(0.0)
            pz_total = np.float32(0.0)
            e_total = np.float32(0.0)
            
            for iobj in range(start, end):
                if mask_objects[iobj]:
                    pt = pts[iobj]
                    eta = etas[iobj]
                    phi = phis[iobj]
                    mass = masses[iobj]

                    px = pt * np.cos(phi)
                    py = pt * np.sin(phi)
                    pz = pt * np.sinh(eta)
                    e = np.sqrt(px**2 + py**2 + pz**2 + mass**2)
                    
                    px_total += px 
                    py_total += py 
                    pz_total += pz 
                    e_total += e

            inv_mass = np.sqrt(-(px_total**2 + py_total**2 + pz_total**2 - e_total**2))
            pt_total = np.sqrt(px_total**2 + py_total**2)
            out_inv_mass[iev] = inv_mass
            out_pt_total[iev] = pt_total

@cuda.jit
def compute_inv_mass_cudakernel(offsets, pts, etas, phis, masses, mask_events, mask_objects, out_inv_mass, out_pt_total):
    xi = cuda.grid(1)
    xstride = cuda.gridsize(1)
    for iev in range(xi, offsets.shape[0]-1, xstride):
        if mask_events[iev]:
            start = np.uint64(offsets[iev])
            end = np.uint64(offsets[iev + 1])
            
            px_total = np.float32(0.0)
            py_total = np.float32(0.0)
            pz_total = np.float32(0.0)
            e_total = np.float32(0.0)
            
            for iobj in range(start, end):
                if mask_objects[iobj]:
                    pt = pts[iobj]
                    eta = etas[iobj]
                    phi = phis[iobj]
                    mass = masses[iobj]

                    px = pt * math.cos(phi)
                    py = pt * math.sin(phi)
                    pz = pt * math.sinh(eta)
                    e = math.sqrt(px**2 + py**2 + pz**2 + mass**2)
                    
                    px_total += px 
                    py_total += py 
                    pz_total += pz 
                    e_total += e

            inv_mass = math.sqrt(-(px_total**2 + py_total**2 + pz_total**2 - e_total**2))
            pt_total = math.sqrt(px_total**2 + py_total**2)
            out_inv_mass[iev] = inv_mass
            out_pt_total[iev] = pt_total

def fill_with_weights(values, weight_dict, mask, bins):
    ret = {}
    vals = values
    for wn in weight_dict.keys():
        _weights = weight_dict[wn]
        ret[wn] = get_histogram(vals, _weights, bins, mask)
    return ret

def update_histograms_systematic(hists, hist_name, systematic_name, target_histogram):

    if hist_name not in hists:
        hists[hist_name] = {}

    if systematic_name[0] == "nominal" or systematic_name == "nominal":
        hists[hist_name].update(target_histogram)
    else:
        if systematic_name[1] == "":
            syst_string = systematic_name[0]
        else:
            syst_string = systematic_name[0] + "__" + systematic_name[1]
        target_histogram = {syst_string: target_histogram["nominal"]}
        hists[hist_name].update(target_histogram)

def remove_inf_nan(arr):
    arr[np.isinf(arr)] = 0
    arr[np.isnan(arr)] = 0
    arr[arr < 0] = 0

def fix_large_weights(weights, maxw=10.0):
    weights[weights > maxw] = maxw
    weights[:] = weights[:] / NUMPY_LIB.mean(weights)

def compute_pu_weights(pu_corrections_target, weights, mc_nvtx, reco_nvtx):
    mc_nvtx = NUMPY_LIB.array(mc_nvtx, dtype=NUMPY_LIB.float32)
    pu_edges, (values_nom, values_up, values_down) = pu_corrections_target

    pu_edges = NUMPY_LIB.array(pu_edges, dtype=NUMPY_LIB.float32)

    src_pu_hist = get_histogram(mc_nvtx, weights, pu_edges)
    norm = sum(src_pu_hist.contents)
    values_target = src_pu_hist.contents/norm

    ratio = values_nom / values_target
    remove_inf_nan(ratio)
    pu_weights = NUMPY_LIB.zeros_like(weights)
    ha.get_bin_contents(reco_nvtx, NUMPY_LIB.array(pu_edges, dtype=NUMPY_LIB.float32),
        NUMPY_LIB.array(ratio, dtype=NUMPY_LIB.float32), pu_weights)
    fix_large_weights(pu_weights) 
     
    ratio_up = values_up / values_target
    remove_inf_nan(ratio_up)
    pu_weights_up = NUMPY_LIB.zeros_like(weights)
    ha.get_bin_contents(reco_nvtx, NUMPY_LIB.array(pu_edges, dtype=NUMPY_LIB.float32),
        NUMPY_LIB.array(ratio_up, dtype=NUMPY_LIB.float32), pu_weights_up)
    fix_large_weights(pu_weights_up) 
    
    ratio_down = values_down / values_target
    remove_inf_nan(ratio_down)
    pu_weights_down = NUMPY_LIB.zeros_like(weights)
    ha.get_bin_contents(reco_nvtx, NUMPY_LIB.array(pu_edges, dtype=NUMPY_LIB.float32),
        NUMPY_LIB.array(ratio_down, dtype=NUMPY_LIB.float32), pu_weights_down)
    fix_large_weights(pu_weights_down) 
    
    return pu_weights, pu_weights_up, pu_weights_down

def select_events_trigger(scalars, parameters, mask_events, hlt_bits):
    flags = [
        "Flag_BadPFMuonFilter",
        "Flag_EcalDeadCellTriggerPrimitiveFilter",
        "Flag_HBHENoiseFilter",
        "Flag_HBHENoiseIsoFilter",
        "Flag_globalSuperTightHalo2016Filter",
        "Flag_goodVertices",
        "Flag_BadChargedCandidateFilter"
    ]
    for flag in flags:
        mask_events = mask_events & scalars[flag]
    
    pvsel = scalars["PV_npvsGood"] > parameters["nPV"]
    pvsel = pvsel & (scalars["PV_ndof"] > parameters["NdfPV"])
    pvsel = pvsel & (scalars["PV_z"] < parameters["zPV"])

    trig_decision = scalars[hlt_bits[0]]
    for hlt_bit in hlt_bits[1:]:
        trig_decision += scalars[hlt_bit]
    trig_decision = trig_decision >= 1
    mask_events = mask_events & trig_decision & pvsel
    return mask_events

def get_int_lumi(runs, lumis, mask_events, lumidata):
    processed_runs = NUMPY_LIB.asnumpy(runs[mask_events])
    processed_lumis = NUMPY_LIB.asnumpy(lumis[mask_events])
    runs_lumis = np.zeros((processed_runs.shape[0], 2), dtype=np.uint32)
    runs_lumis[:, 0] = processed_runs[:]
    runs_lumis[:, 1] = processed_lumis[:]
    lumi_proc = lumidata.get_lumi(runs_lumis)
    return lumi_proc

def get_gen_sumweights(filenames):
    sumw = 0
    sumw2 = 0
    for fi in filenames:
        ff = uproot.open(fi)
        bl = ff.get("Runs")
        arr = bl.array("genEventSumw")
        arr2 = bl.array("genEventSumw2")
        arr = arr
        sumw += arr.sum()
        sumw2 += arr2.sum()
    return sumw, sumw2

"""
Applies Rochester corrections on leading and subleading muons, returns the corrected pt

    is_mc: bool
    rochester_corrections: RochesterCorrections object that contains calibration data
    muons: JaggedStruct of all muon data

    returns: nothing

"""
def do_rochester_corrections(
    is_mc,
    rochester_corrections,
    muons):

    qterm = rochester_correction_muon_qterm(
        is_mc, rochester_corrections, muons)
    
    muon_pt_corr = muons.pt * qterm
    muons.pt[:] = muon_pt_corr[:]

    return

"""
Computes the Rochester correction q-term for an array of muons.

    is_mc: bool
    rochester_corrections: RochesterCorrections object that contains calibration data
    muons: JaggedStruct of all muon data

    returns: array of the computed q-term values
"""
def rochester_correction_muon_qterm(
    is_mc, rochester_corrections,
    muons):
    if is_mc:
        rnd = NUMPY_LIB.random.rand(len(muons.pt)).astype(NUMPY_LIB.float32)
        qterm = rochester_corrections.compute_kSpreadMC_or_kSmearMC(
            NUMPY_LIB.asnumpy(muons.pt),
            NUMPY_LIB.asnumpy(muons.eta),
            NUMPY_LIB.asnumpy(muons.phi),
            NUMPY_LIB.asnumpy(muons.charge),
            NUMPY_LIB.asnumpy(muons.genpt),
            NUMPY_LIB.asnumpy(muons.nTrackerLayers),
            NUMPY_LIB.asnumpy(rnd)
        )
    else:
        qterm = rochester_corrections.compute_kScaleDT(
            NUMPY_LIB.asnumpy(muons.pt),
            NUMPY_LIB.asnumpy(muons.eta),
            NUMPY_LIB.asnumpy(muons.phi),
            NUMPY_LIB.asnumpy(muons.charge),
        )

    return NUMPY_LIB.array(qterm)

@numba.njit('float32[:], float32[:], float32[:]', parallel=True, fastmath=True)
def deltaphi_cpu(phi1, phi2, out_dphi):
    for iev in numba.prange(len(phi1)):
        dphi = phi1[iev] - phi2[iev] 
        if dphi > math.pi:
            dphi = dphi - 2*math.pi
            out_dphi[iev] = dphi
        elif (dphi + math.pi) < 0:
            dphi = dphi + 2*math.pi
            out_dphi[iev] = dphi
        else:
            out_dphi[iev] = dphi

@cuda.jit
def deltaphi_cudakernel(phi1, phi2, out_dphi):
    xi = cuda.grid(1)
    xstride = cuda.gridsize(1)
    
    for iev in range(xi, len(phi1), xstride):
        dphi = phi1[iev] - phi2[iev] 
        if dphi > math.pi:
            dphi = dphi - 2*math.pi
            out_dphi[iev] = dphi
        elif (dphi + math.pi) < 0:
            dphi = dphi + 2*math.pi
            out_dphi[iev] = dphi
        else:
            out_dphi[iev] = dphi

@numba.njit(parallel=True, fastmath=True)
def get_theoryweights_cpu(offsets, variations, index, out_var):
    #loop over events
    for iev in numba.prange(len(offsets) - 1):
        out_var[iev] = variations[offsets[iev]+index]

# Custom kernels to get the pt of the genHiggs
def genhpt(nevt,genpart, mask):
    assert(mask.shape == genpart.status.shape)
    mask_out = NUMPY_LIB.zeros(nevt, dtype=NUMPY_LIB.float32)
    genhpt_cpu(
        nevt, genpart.offsets, genpart.pdgId, genpart.status, genpart.pt, mask, mask_out
    )
    return mask_out

@numba.njit(parallel=True, fastmath=True)
def genhpt_cpu(nevt, genparts_offsets, pdgid, status, pt, mask, out_genhpt):
    #loop over events
    for iev in numba.prange(nevt):
        #print("ievt: ",iev)
        gen_Higgs_pt = -1;
        #loop over genpart
        for igenpart in range(genparts_offsets[iev], genparts_offsets[iev + 1]):
            if mask[igenpart]:
                #print("pdgid stats: ", pdgid[igenpart], status[igenpart])
                gen_Higgs_pt = pt[igenpart]
                #print("gen_Higgs_pt: ",gen_Higgs_pt)
                break 
        #print("final gen_Higgs_pt: ",gen_Higgs_pt)
        out_genhpt[iev] = gen_Higgs_pt

# Custom kernels to get the number of genJets with pT>30 GEV
def gennjets(nevt,genjets, ptcut):
    njet_out = NUMPY_LIB.zeros(nevt, dtype=NUMPY_LIB.int32)
    gennjets_cpu(nevt, genjets.offsets,genjets.pt, ptcut, njet_out)
    return njet_out   

@numba.njit(parallel=True, fastmath=True)
def gennjets_cpu(nevt, genjets_offsets,pt, ptcut, njet_out):
    for iev in numba.prange(nevt):
        njet = 0
        for igenjets in range(genjets_offsets[iev], genjets_offsets[iev + 1]):
            if pt[igenjets] > ptcut:
                njet += 1
        njet_out[iev] = njet

# Custom kernels to get the pt of the muon based on the matched genPartIdx of the reco muon
# Implement them here as they are too specific to NanoAOD for the hepaccelerate library
@numba.njit(parallel=True, fastmath=True)
def get_genpt_cpu(reco_offsets, reco_genPartIdx, genparts_offsets, genparts_pt, out_reco_genpt):
    #loop over events
    for iev in numba.prange(len(reco_offsets) - 1):
        #loop over muons
        for imu in range(reco_offsets[iev], reco_offsets[iev + 1]):
            #get index of genparticle that reco particle was matched to
            idx_gp = reco_genPartIdx[imu]
            if idx_gp >= 0:
                genpt = genparts_pt[genparts_offsets[iev] + idx_gp]
                out_reco_genpt[imu] = genpt

@cuda.jit
def get_genpt_cuda(reco_offsets, reco_genPartIdx, genparts_offsets, genparts_pt, out_reco_genpt):
    #loop over events
    xi = cuda.grid(1)
    xstride = cuda.gridsize(1)
    
    for iev in range(xi, len(reco_offsets) - 1, xstride):
        #loop over muons
        for imu in range(reco_offsets[iev], reco_offsets[iev + 1]):
            #get index of genparticle that reco particle was matched to
            idx_gp = reco_genPartIdx[imu]
            if idx_gp >= 0:
                genpt = genparts_pt[genparts_offsets[iev] + idx_gp]
                out_reco_genpt[imu] = genpt

@numba.njit(parallel=True, fastmath=True)
def get_matched_genparticles(reco_offsets, reco_genPartIdx, mask_reco, genparts_offsets, out_genparts_mask):
    #loop over events
    for iev in numba.prange(len(reco_offsets) - 1):
        #loop over reco objects
        for iobj in range(reco_offsets[iev], reco_offsets[iev + 1]):
            if not mask_reco[iobj]:
                continue
            #get index of genparticle that muon was matched to
            idx_gp_ev = reco_genPartIdx[iobj]
            if idx_gp_ev >= 0:
                idx_gp = int(genparts_offsets[iev]) + int(idx_gp_ev)
                out_genparts_mask[idx_gp] = True

@cuda.jit
def get_matched_genparticles_kernel(reco_offsets, reco_genPartIdx, mask_reco, genparts_offsets, out_genparts_mask):
    #loop over events
    xi = cuda.grid(1)
    xstride = cuda.gridsize(1)
    
    for iev in range(xi, len(reco_offsets) - 1, xstride):
        #loop over reco objects
        for iobj in range(reco_offsets[iev], reco_offsets[iev + 1]):
            if not mask_reco[iobj]:
                continue
            #get index of genparticle that muon was matched to
            idx_gp_ev = reco_genPartIdx[iobj]
            if idx_gp_ev >= 0:
                idx_gp = int(genparts_offsets[iev]) + int(idx_gp_ev)
                out_genparts_mask[idx_gp] = True


def to_cartesian(arrs):
    pt = arrs["pt"]
    eta = arrs["eta"]
    phi = arrs["phi"]
    mass = arrs["mass"]
    px = pt * NUMPY_LIB.cos(phi)
    py = pt * NUMPY_LIB.sin(phi)
    pz = pt * NUMPY_LIB.sinh(eta)
    e = NUMPY_LIB.sqrt(px**2 + py**2 + pz**2 + mass**2)
    return {"px": px, "py": py, "pz": pz, "e": e}

def rapidity(e, pz):
    return 0.5*NUMPY_LIB.log((e + pz) / (e - pz))

"""
Given a a dictionary of arrays of cartesian coordinates (px, py, pz, e),
computes the array of spherical coordinates (pt, eta, phi, m)

    arrs: dict of str -> array
    returns: dict of str -> array
"""
def to_spherical(arrs):
    px = arrs["px"]
    py = arrs["py"]
    pz = arrs["pz"]
    e = arrs["e"]
    pt = NUMPY_LIB.sqrt(px**2 + py**2)
    eta = NUMPY_LIB.arcsinh(pz / pt)
    phi = NUMPY_LIB.arccos(NUMPY_LIB.clip(px / pt, -1.0, 1.0))
    mass = NUMPY_LIB.sqrt(NUMPY_LIB.abs(e**2 - (px**2 + py**2 + pz**2)))
    rap = rapidity(e, pz)
    return {"pt": pt, "eta": eta, "phi": phi, "mass": mass, "rapidity": rap}

"""
Given two objects, computes the dr = sqrt(deta^2+dphi^2) between them.
    obj1: array of spherical coordinates (pt, eta, phi, m) for the first object
    obj2: array of spherical coordinates for the second object

    returns: arrays of deta, dphi, dr
"""
def deltar(obj1, obj2, use_cuda):
    deta = obj1["eta"] - obj2["eta"]
    dphi = NUMPY_LIB.zeros(len(deta), dtype=NUMPY_LIB.float32)
    if use_cuda:
        deltaphi_cudakernel[21,1024](obj1["phi"],obj2["phi"],dphi)
        cuda.synchronize()
    else:
        deltaphi_cpu(obj1["phi"],obj2["phi"],dphi)
    dr = NUMPY_LIB.sqrt(deta**2 + dphi**2)
    return deta, dphi, dr 

"""
Fills the DNN input variables based on two muons and two jets.
    leading_muon: spherical coordinate data of the leading muon
    subleading_muon: spherical coordinate data of the subleading muon
    leading_jet: spherical coordinate data + QGL of the leading jet
    subleading_jet: spherical coordinate data + QGL of the subleading jet
    nsoft: number of soft jets
    
    'softJet5' - # of soft EWK jets with pt > 5 GeV
    'dRmm' - Delta R between two muons
    'dEtamm' - Delta eta between two muons
    'dPhimm' - Delta Phi between two muons
    'M_jj' - dijet mass
    'pt_jj' - dijet pt
    'eta_jj' - dijet eta
    'phi_jj' - dijet phi
    'M_mmjj' - mass of dimuon + dijet system
    'eta_mmjj' - eta of dimuon + dijet system
    'phi_mmjj' - phi of dimuon + dijet system
    'dEta_jj' - delta eta between two jets
    'Zep' - zeppenfeld variable with pseudorapidity
    'dRmin_mj' - Min delta R between a muon and jet
    'dRmax_mj' - Max delta R between a muon and jet
    'dRmin_mmj' - Min delta R between dimuon and jet
    'dRmax_mmj' - Max delta R between dimuon and jet
    'leadingJet_pt' - Leading jet pt
    'subleadingJet_pt' - sub-leading jet pt 
    'leadingJet_eta' - leading jet eta
    'subleadingJet_eta' - sub-leading jet eta
    'leadingJet_qgl' - leading jet qgl
    'subleadingJet_qgl' - sub - leading jet qgl
    'cthetaCS' - cosine of collins Soper frame angle
    'Higgs_pt' - dimuon pt
    'Higgs_eta' - dimuon eta
"""
def dnn_variables(hrelresolution, leading_muon, subleading_muon, leading_jet, subleading_jet, nsoft, use_cuda):
    #delta eta, phi and R between two muons
    mm_deta, mm_dphi, mm_dr = deltar(leading_muon, subleading_muon, use_cuda)
    
    #delta eta between jets 
    jj_deta = leading_jet["eta"] - subleading_jet["eta"]
    jj_dphi = leading_jet["phi"] - subleading_jet["phi"]
    jj_dphi_mod = NUMPY_LIB.zeros(len(jj_dphi), dtype=NUMPY_LIB.float32)

    if use_cuda:
        deltaphi_cudakernel[32,1024](leading_jet["phi"],subleading_jet["phi"], jj_dphi_mod)
        cuda.synchronize()
    else:
        deltaphi_cpu(leading_jet["phi"],subleading_jet["phi"], jj_dphi_mod)

    #jj_dphi_mod = NUMPY_LIB.mod(jj_dphi + math.pi, math.pi)
    
    #muons in cartesian, create dimuon system 
    m1 = to_cartesian(leading_muon)    
    m2 = to_cartesian(subleading_muon)    
    mm = {k: m1[k] + m2[k] for k in ["px", "py", "pz", "e"]}
    mm_sph = to_spherical(mm)

    #mass resolusion
    Higgs_mrelreso = hrelresolution.compute(leading_muon["pt"],leading_muon["eta"],subleading_muon["pt"],subleading_muon["eta"])

    #jets in cartesian, create dijet system 
    j1 = to_cartesian(leading_jet)
    j2 = to_cartesian(subleading_jet)
    leading_jet["rapidity"] = rapidity(j1["e"], j1["pz"]) 
    subleading_jet["rapidity"] = rapidity(j2["e"], j2["pz"]) 
    jj = {k: j1[k] + j2[k] for k in ["px", "py", "pz", "e"]}
    jj_sph = to_spherical(jj)
  
    #create dimuon-dijet system 
    mmjj = {k: j1[k] + j2[k] + m1[k] + m2[k] for k in ["px", "py", "pz", "e"]} 
    mmjj_sph = to_spherical(mmjj)
    #compute deletaEta between Higgs and jet
    EtaHQs = []
    for jet in [leading_jet, subleading_jet]:
        EtaHQ = mm_sph["eta"] - jet["eta"] 
        EtaHQs += [EtaHQ]
    EtaHQ = NUMPY_LIB.vstack(EtaHQs)
    minEtaHQ = NUMPY_LIB.min(EtaHQ, axis=0)
    #compute deltaR between all muons and jets
    dr_mjs = []
    for mu in [leading_muon, subleading_muon]:
        for jet in [leading_jet, subleading_jet]:
            _, _, dr_mj = deltar(mu, jet, use_cuda)
            dr_mjs += [dr_mj]
    dr_mj = NUMPY_LIB.vstack(dr_mjs)
    dRmin_mj = NUMPY_LIB.min(dr_mj, axis=0) 
    dRmax_mj = NUMPY_LIB.max(dr_mj, axis=0) 
    #compute deltaR between dimuon system and both jets 
    dr_mmjs = []
    for jet in [leading_jet, subleading_jet]:
        _, _, dr_mmj = deltar(mm_sph, jet, use_cuda)
        dr_mmjs += [dr_mmj]
    dr_mmj = NUMPY_LIB.vstack(dr_mmjs)
    dRmin_mmj = NUMPY_LIB.min(dr_mmj, axis=0) 
    dRmax_mmj = NUMPY_LIB.max(dr_mmj, axis=0)
   
    #Zeppenfeld variable
    Zep = (mm_sph["eta"] - 0.5*(leading_jet["eta"] + subleading_jet["eta"]))
    Zep_rapidity = (mm_sph["rapidity"] - 0.5*(leading_jet["rapidity"] + subleading_jet["rapidity"]))/(leading_jet["rapidity"]-subleading_jet["rapidity"])

    #Collin-Soper frame variable
    cthetaCS = 2*(m1["pz"] * m2["e"] - m1["e"]*m2["pz"]) / (mm_sph["mass"] * NUMPY_LIB.sqrt(NUMPY_LIB.power(mm_sph["mass"], 2) + NUMPY_LIB.power(mm_sph["pt"], 2)))

    ret = {
        #"leading_muon_pt": leading_muon["pt"],
        #"leading_muon_eta": leading_muon["eta"],
        #"leading_muon_phi": leading_muon["phi"],
        #"leading_muon_mass": leading_muon["mass"],
        #"subleading_muon_pt": subleading_muon["pt"],
        #"subleading_muon_eta": subleading_muon["eta"],
        #"subleading_muon_phi": subleading_muon["phi"],
        #"subleading_muon_mass": subleading_muon["mass"],
        "dEtamm": mm_deta, "dPhimm": mm_dphi, "dRmm": mm_dr,
        "M_jj": jj_sph["mass"], "pt_jj": jj_sph["pt"], "eta_jj": jj_sph["eta"], "phi_jj": jj_sph["phi"],
        "M_mmjj": mmjj_sph["mass"], "eta_mmjj": mmjj_sph["eta"], "phi_mmjj": mmjj_sph["phi"],
        "dEta_jj": jj_deta,
        "dEta_jj_abs": NUMPY_LIB.abs(jj_deta),
        "dPhi_jj": jj_dphi,
        "dPhi_jj_mod": jj_dphi_mod,
        "dPhi_jj_mod_abs": NUMPY_LIB.abs(jj_dphi_mod),
        "leadingJet_pt": leading_jet["pt"],
        "subleadingJet_pt": subleading_jet["pt"],
        "leadingJet_eta": leading_jet["eta"],
        "subleadingJet_eta": subleading_jet["eta"],
        "dRmin_mj": dRmin_mj,
        "dRmax_mj": dRmax_mj,
        "dRmin_mmj": dRmin_mmj,
        "dRmax_mmj": dRmax_mmj,
        "Zep": Zep,
        "Zep_rapidity": Zep_rapidity,
        "leadingJet_qgl": leading_jet["qgl"],
        "subleadingJet_qgl": subleading_jet["qgl"], 
        "cthetaCS": cthetaCS,
        "softJet5": nsoft,
        "Higgs_pt": mm_sph["pt"],
        "Higgs_eta": mm_sph["eta"],
        "Higgs_rapidity": mm_sph["rapidity"],
        "Higgs_mass": mm_sph["mass"],
        #DNN pisa variable
        "Mqq_log": NUMPY_LIB.log(jj_sph["mass"] ),
        "Rpt": mmjj_sph["pt"]/(mm_sph["pt"]+jj_sph["pt"]),
        "qqDeltaEta": NUMPY_LIB.abs(jj_deta),
        "ll_zstar": NUMPY_LIB.abs(mm_sph["rapidity"] - 0.5*(leading_jet["rapidity"] + subleading_jet["rapidity"]))/(leading_jet["rapidity"]-subleading_jet["rapidity"]),
        "NSoft5": nsoft,
        "minEtaHQ": minEtaHQ,
        "log(Higgs_pt)": NUMPY_LIB.log(mm_sph["pt"]),
        "Mqq": jj_sph["mass"],
        "QJet0_pt_touse": leading_jet["pt"],
        "QJet1_pt_touse": subleading_jet["pt"],
        "QJet0_eta": leading_jet["eta"],
        "QJet1_eta": subleading_jet["eta"],
        "QJet0_phi": leading_jet["phi"],
        "QJet1_phi": subleading_jet["phi"],
        "QJet0_qgl": leading_jet["qgl"],
        "QJet1_qgl": subleading_jet["qgl"],
        "Higgs_m": mm_sph["mass"],
        "Higgs_mRelReso": Higgs_mrelreso,
        "Higgs_mReso": mm_sph["mass"]*Higgs_mrelreso,
    }

    if debug:
        for k in ret.keys():
            msk = NUMPY_LIB.isnan(ret[k])
            if NUMPY_LIB.sum(msk) > 0:
                print("dnn vars nan", k, np.sum(msk))

    return ret

"""
Given an dictionary with arrays and a mask, applies the mask on all arrays
    arr_dict: dict of key -> array for input data
    mask: mask with the same length as the arrays
"""
def apply_mask(arr_dict, mask):
    return {k: v[mask] for k, v in arr_dict.items()}

def select_weights(weights, jet_systematic_scenario):
    if jet_systematic_scenario[0] == "nominal":
        return weights
    else:
        return {"nominal": weights["nominal"]}

# 1. Compute the DNN input variables in a given preselection
# 2. Evaluate the DNN model
# 3. Fill histograms with DNN inputs and output
def compute_fill_dnn(
    hrelresolution,
    miscvariables,
    parameters,
    use_cuda,
    dnn_presel,
    dnn_model,
    dnn_normfactors,
    dnnPisa_model,
    dnnPisa_normfactors1,
    dnnPisa_normfactors2,
    scalars,
    leading_muon,
    subleading_muon,
    leading_jet,
    subleading_jet,
    num_jets,
    num_jets_btag,
    dataset_era):

    nev_dnn_presel = NUMPY_LIB.sum(dnn_presel)

    #for some reason, on the cuda backend, the sum does not return a simple number
    if use_cuda:
        nev_dnn_presel = int(NUMPY_LIB.asnumpy(nev_dnn_presel).flatten()[0])

    leading_muon_s = apply_mask(leading_muon, dnn_presel)
    subleading_muon_s = apply_mask(subleading_muon, dnn_presel)
    leading_jet_s = apply_mask(leading_jet, dnn_presel)
    subleading_jet_s = apply_mask(subleading_jet, dnn_presel)
    nsoft = scalars["SoftActivityJetNjets5"][dnn_presel]

    dnn_vars = dnn_variables(hrelresolution, leading_muon_s, subleading_muon_s, leading_jet_s, subleading_jet_s, nsoft, use_cuda)
    if dataset_era == "2017":
    	dnn_vars["MET_pt"] = scalars["METFixEE2017_pt"][dnn_presel]
    else:
    	dnn_vars["MET_pt"] = scalars["MET_pt"][dnn_presel]
    dnn_vars["num_jets"] = num_jets[dnn_presel]
    dnn_vars["num_jets_btag"] = num_jets_btag[dnn_presel]
 
    if (not (dnn_model is None)) and nev_dnn_presel > 0:
        #print("dnn_model: ",dnn_model)
        #print("dnn_normfactors[0][1]: ", dnn_normfactors[0], dnn_normfactors[1])
        dnn_vars_arr = NUMPY_LIB.vstack([dnn_vars[k] for k in parameters["dnn_varlist_order"]]).T
        
        #Normalize the DNN with the normalization factors from preprocessing in training 
        dnn_vars_arr -= dnn_normfactors[0]
        dnn_vars_arr /= dnn_normfactors[1]

        #for TF, need to convert library to numpy, as it doesn't accept cupy arrays
        dnn_pred = NUMPY_LIB.array(dnn_model.predict(
            NUMPY_LIB.asnumpy(dnn_vars_arr),
            batch_size=dnn_vars_arr.shape[0])[:, 0]
        )
        if len(dnn_pred) > 0:
            print("dnn_pred", dnn_pred.min(), dnn_pred.max(), dnn_pred.mean(), dnn_pred.std())
        dnn_pred = NUMPY_LIB.array(dnn_pred, dtype=NUMPY_LIB.float32)
    else:
        dnn_pred = NUMPY_LIB.zeros(nev_dnn_presel, dtype=NUMPY_LIB.float32)

    #Pisa DNN
    if parameters["do_dnn_pisa"]:
        if (not (dnnPisa_model is None)) and nev_dnn_presel > 0:    
            dnnPisa_vars1_arr = NUMPY_LIB.vstack([dnn_vars[k] for k in parameters["dnnPisa_varlist1_order"]]).T
            dnnPisa_vars2_arr = NUMPY_LIB.vstack([dnn_vars[k] for k in parameters["dnnPisa_varlist2_order"]]).T
            dnnPisa_vars1_arr += dnnPisa_normfactors1[0]
            dnnPisa_vars1_arr *= dnnPisa_normfactors1[1]
            dnnPisa_vars2_arr += dnnPisa_normfactors2[0] 
            dnnPisa_vars2_arr *= dnnPisa_normfactors2[1]
            dnnPisa_pred = NUMPY_LIB.array(dnnPisa_model.predict(
                [NUMPY_LIB.asnumpy(dnnPisa_vars1_arr),NUMPY_LIB.asnumpy(dnnPisa_vars2_arr)],
                batch_size=dnnPisa_vars1_arr.shape[0])[:, 0]
            )
            if len(dnnPisa_pred) > 0:
                print("dnnPisa_pred", dnnPisa_pred.min(), dnnPisa_pred.max(), dnnPisa_pred.mean(), dnnPisa_pred.std())
            dnnPisa_pred = NUMPY_LIB.array(dnnPisa_pred, dtype=NUMPY_LIB.float32)
        else:
            dnnPisa_pred = NUMPY_LIB.zeros(nev_dnn_presel, dtype=NUMPY_LIB.float32)
 
    if parameters["do_bdt_ucsd"]:
        hmmthetacs, hmmphics = miscvariables.csangles(
            NUMPY_LIB.asnumpy(leading_muon_s["pt"]),
            NUMPY_LIB.asnumpy(leading_muon_s["eta"]),
            NUMPY_LIB.asnumpy(leading_muon_s["phi"]),
            NUMPY_LIB.asnumpy(leading_muon_s["mass"]),
            NUMPY_LIB.asnumpy(subleading_muon_s["pt"]),
            NUMPY_LIB.asnumpy(subleading_muon_s["eta"]),
            NUMPY_LIB.asnumpy(subleading_muon_s["phi"]),
            NUMPY_LIB.asnumpy(subleading_muon_s["mass"]),
            NUMPY_LIB.asnumpy(leading_muon_s["charge"]),
            )
        dnn_vars["hmmthetacs"] = NUMPY_LIB.array(hmmthetacs)
        dnn_vars["hmmphics"] = NUMPY_LIB.array(hmmphics)
    
    # event-by-event mass resolution
    dpt1 = (leading_muon_s["ptErr"]*dnn_vars["Higgs_mass"]) / (2*leading_muon_s["pt"])
    dpt2 = (subleading_muon_s["ptErr"]*dnn_vars["Higgs_mass"]) / (2*subleading_muon_s["pt"])
    mm_massErr = NUMPY_LIB.sqrt(dpt1*dpt1 +dpt2*dpt2)
    dnn_vars["massErr"] = mm_massErr
    dnn_vars["massErr_rel"] = mm_massErr / dnn_vars["Higgs_mass"]

    dnn_vars["m1eta"] = NUMPY_LIB.array(leading_muon_s["eta"])
    dnn_vars["m2eta"] = NUMPY_LIB.array(subleading_muon_s["eta"])
    dnn_vars["m1ptOverMass"] = NUMPY_LIB.divide(leading_muon_s["pt"],dnn_vars["Higgs_mass"])
    dnn_vars["m2ptOverMass"] = NUMPY_LIB.divide(subleading_muon_s["pt"],dnn_vars["Higgs_mass"])
    return dnn_vars, dnn_pred, dnnPisa_pred

def get_jer_smearfactors(pt_or_m, ratio_jet_genjet, msk_no_genjet, msk_poor_reso, resos, resosfs):
    
    #smearing for matched jets
    smear_matched_n = 1.0 + (resosfs[:, 0] - 1.0) * ratio_jet_genjet
    smear_matched_u = 1.0 + (resosfs[:, 1] - 1.0) * ratio_jet_genjet
    smear_matched_d = 1.0 + (resosfs[:, 2] - 1.0) * ratio_jet_genjet

    #compute random smearing for unmatched jets
    sigma_unmatched_n = resos * NUMPY_LIB.sqrt(NUMPY_LIB.clip(resosfs[:, 0]**2 - 1.0, 0, 100))
    sigma_unmatched_u = resos * NUMPY_LIB.sqrt(NUMPY_LIB.clip(resosfs[:, 1]**2 - 1.0, 0, 100))
    sigma_unmatched_d = resos * NUMPY_LIB.sqrt(NUMPY_LIB.clip(resosfs[:, 2]**2 - 1.0, 0, 100))

    zeros = NUMPY_LIB.ones_like(sigma_unmatched_n)
    rand = NUMPY_LIB.random.normal(loc=zeros, scale=resos, size=len(zeros))
    
    smear_rnd_n = 1. + rand * NUMPY_LIB.sqrt(resosfs[:, 0]**2 - 1.)
    smear_rnd_u = 1. + rand * NUMPY_LIB.sqrt(resosfs[:, 1]**2 - 1.)
    smear_rnd_d = 1. + rand * NUMPY_LIB.sqrt(resosfs[:, 2]**2 - 1.)

    inds_no_genjet = NUMPY_LIB.nonzero(msk_no_genjet)[0]

    smear_n = NUMPY_LIB.array(smear_matched_n)
    smear_u = NUMPY_LIB.array(smear_matched_u)
    smear_d = NUMPY_LIB.array(smear_matched_d)

    #for jets that have no matched genjet, use random smearing
    ha.copyto_dst_indices(smear_n, smear_rnd_n[msk_no_genjet], inds_no_genjet)
    ha.copyto_dst_indices(smear_u, smear_rnd_u[msk_no_genjet], inds_no_genjet)
    ha.copyto_dst_indices(smear_d, smear_rnd_d[msk_no_genjet], inds_no_genjet)

    smear_n[msk_no_genjet & (resosfs[:, 0]<1.0)] = 1
    smear_u[msk_no_genjet & (resosfs[:, 1]<1.0)] = 1
    smear_d[msk_no_genjet & (resosfs[:, 2]<1.0)] = 1

    smear_n[(smear_n * pt_or_m) < 0.01] = 0.01
    smear_u[(smear_u * pt_or_m) < 0.01] = 0.01
    smear_d[(smear_d * pt_or_m) < 0.01] = 0.01

    return smear_n, smear_u, smear_d, sigma_unmatched_n


class JetTransformer:
    def __init__(self, jets, scalars, parameters, jetmet_corrections, NUMPY_LIB, use_cuda, is_mc):
        self.jets = jets
        self.scalars = scalars
        self.jetmet_corrections = jetmet_corrections
        self.NUMPY_LIB = NUMPY_LIB
        self.use_cuda = use_cuda
        self.is_mc = is_mc

        self.jets_rho = NUMPY_LIB.zeros_like(jets.pt)
        ha.broadcast(scalars["fixedGridRhoFastjetAll"], self.jets.offsets, self.jets_rho)
        
        # Get the uncorrected jet pt and mass
        self.raw_pt = (self.jets.pt * (1.0 - self.jets.rawFactor))
        self.raw_mass = (self.jets.mass * (1.0 - self.jets.rawFactor))

        self.jet_uncertainty_names = list(self.jetmet_corrections.jesunc.levels)
        self.jet_uncertainty_names.pop(self.jet_uncertainty_names.index("jes"))

        # Need to use the CPU for JEC/JER currently
        if self.use_cuda:
            self.raw_pt = NUMPY_LIB.asnumpy(self.raw_pt)
            self.eta = NUMPY_LIB.asnumpy(self.jets.eta)
            self.rho = NUMPY_LIB.asnumpy(self.jets_rho)
            self.area = NUMPY_LIB.asnumpy(self.jets.area)
        else:
            self.raw_pt = self.raw_pt
            self.eta = self.jets.eta
            self.rho = self.jets_rho
            self.area = self.jets.area

        if self.is_mc:
            self.corr_jec = self.apply_jec_mc()
        else:
            self.corr_jec = self.apply_jec_data()

        self.corr_jec = NUMPY_LIB.array(self.corr_jec)
        self.pt_jec = NUMPY_LIB.array(self.raw_pt) * self.corr_jec 

    def apply_jer(self):
        #This is done only on CPU
            resos = self.jetmet_corrections.jer.getResolution(
                JetEta=eta, Rho=rho, JetPt=NUMPY_LIB.asnumpy(pt_jec))
            resosfs = self.jetmet_corrections.jersf.getScaleFactor(JetEta=eta)

            #The following is done either on CPU or GPU
            resos = NUMPY_LIB.array(resos)
            resosfs = NUMPY_LIB.array(resosfs)

            dpt_jet_genjet = jets.pt - jets.genpt
            dpt_jet_genjet[jets.genpt == 0] = 0
            ratio_jet_genjet_pt = dpt_jet_genjet / jets.pt

            msk_no_genjet = ratio_jet_genjet_pt == 0
            msk_poor_reso = resosfs[:, 0] < 1

            dm_jet_genjet = jets.mass - jets.genmass
            dm_jet_genjet[jets.genmass == 0] = 0
            ratio_jet_genjet_mass = dm_jet_genjet / jets.mass
           
            smear_n, smear_u, smear_d, sigma_unmatched_n = get_jer_smearfactors(
                jets.pt, ratio_jet_genjet_pt, msk_no_genjet, msk_poor_reso, resos, resosfs)

    def apply_jec_mc(self):
        corr = self.jetmet_corrections.jec_mc.getCorrection(
            JetPt=self.raw_pt.copy(),
            Rho=self.rho,
            JetEta=self.eta,
            JetA=self.area)
        return corr

    def apply_jec_data(self):
        final_corr = NUMPY_LIB.zeros_like(self.jets.pt)

        #final correction is run-dependent, compute that for each run separately
        for run_idx in NUMPY_LIB.unique(self.scalars["run_index"]):
            
            if self.use_cuda:
                run_idx = int(run_idx)
            msk = self.scalars["run_index"] == run_idx
            
            #find the jets in the events that pass this run index cut
            jets_msk = NUMPY_LIB.zeros(self.jets.numobjects(), dtype=NUMPY_LIB.bool)
            ha.broadcast(msk, self.jets.offsets, jets_msk)
            inds_nonzero = NUMPY_LIB.nonzero(jets_msk)[0]

            #Evaluate jet correction (on CPU only currently)
            if self.use_cuda:
                jets_msk = NUMPY_LIB.asnumpy(jets_msk)
            run_name = runmap_numerical_r[run_idx]

            corr = self.jetmet_corrections.jec_data[run_name].getCorrection(
                JetPt=self.raw_pt[jets_msk].copy(),
                Rho=self.rho[jets_msk],
                JetEta=self.eta[jets_msk],
                JetA=self.area[jets_msk])
            if debug:
                print("run_idx=", run_idx, corr.mean(), corr.std())

            #update the final jet correction for the jets in the events in this run
            if len(inds_nonzero) > 0:
                ha.copyto_dst_indices(final_corr, corr, inds_nonzero)
        corr = final_corr
        return corr

    def apply_jec_unc(self, startfrom="pt_jec", uncertainty_name="Total"):
        ptvec = getattr(self, startfrom)

        idx_func = self.jetmet_corrections.jesunc.levels.index(uncertainty_name)
        jec_unc_func = self.jetmet_corrections.jesunc._funcs[idx_func]
        function_signature = self.jetmet_corrections.jesunc._funcs[idx_func].signature

        args = {
            "JetPt": NUMPY_LIB.array(ptvec),
            "JetEta": NUMPY_LIB.array(self.eta)
        }

        jec_unc_vec = jec_unc_func(*tuple([args[s] for s in function_signature]))
        return NUMPY_LIB.array(jec_unc_vec)

    def get_variated_pts(self, variation_name):
        if variation_name in self.jet_uncertainty_names:
            startfrom = "pt_jec"
            corrs_up_down = NUMPY_LIB.array(self.apply_jec_unc(startfrom, variation_name), dtype=NUMPY_LIB.float32)
            ptvec = getattr(self, startfrom)
            return {
                (variation_name, "up"): ptvec*corrs_up_down[:, 0],
                (variation_name, "down"): ptvec*corrs_up_down[:, 1]
            }
        elif variation_name == "nominal":
            return {("nominal", ""): self.pt_jec}
        else:
            raise KeyError("Variation name {0} was not defined in JetMetCorrections corrections".format(variation_name))

def multiply_all(weight_list):
    ret = NUMPY_LIB.copy(weight_list[0])
    for w in weight_list[1:]:
        ret *= w
    return ret

def compute_lepton_sf(leading_muon, subleading_muon, lepsf_iso, lepsf_id, lepsf_trig, use_cuda, dataset_era, NUMPY_LIB, debug):
    sfs_id = []
    sfs_iso = []
    sfs_trig = []
    sfs_id_up = []
    sfs_id_down = []
    sfs_iso_up = []
    sfs_iso_down = []
    sfs_trig_up = []
    sfs_trig_down = []

    #compute weight for both leading and subleading muon
    for mu in [leading_muon, subleading_muon]:
        #lepton SF computed on CPU 
        if use_cuda:
            mu = {k: NUMPY_LIB.asnumpy(v) for k, v in mu.items()}
        pdgid = numpy.array(mu["pdgId"])
        
        #In 2016, the histograms are flipped
        if dataset_era == "2016":
            pdgid[:] = 11

        sf_iso = lepsf_iso.compute(pdgid, mu["pt"], mu["eta"])
        sf_iso_err = lepsf_iso.compute_error(pdgid, mu["pt"], mu["eta"])

        sf_id = lepsf_id.compute(pdgid, mu["pt"], mu["eta"])
        sf_id_err = lepsf_id.compute_error(pdgid, mu["pt"], mu["eta"])

        if dataset_era == "2016":
            sf_trig = lepsf_trig.compute(pdgid, mu["pt"], NUMPY_LIB.abs(mu["eta"]))
            sf_trig_err = lepsf_trig.compute_error(pdgid, mu["pt"], NUMPY_LIB.abs(mu["eta"]))
        else:
            sf_trig = lepsf_trig.compute(pdgid, mu["pt"], mu["eta"])
            sf_trig_err = lepsf_trig.compute_error(pdgid, mu["pt"], mu["eta"])

        sf_id_up = (sf_id + sf_id_err)
        sf_id_down = (sf_id - sf_id_err)
        sf_iso_up = (sf_iso + sf_iso_err)
        sf_iso_down = (sf_iso - sf_iso_err)
        sf_trig_up = (sf_trig + sf_trig_err)
        sf_trig_down = (sf_trig - sf_trig_err)

        if debug:
            print("sf_iso: ", sf_iso.mean(), "+-", sf_iso.std())
            print("sf_id: ", sf_id.mean(), "+-", sf_id.std())
            print("sf_trig: ", sf_trig.mean(), "+-", sf_trig.std())
            print("sf_id_up: ", sf_id_up.mean(), "+-", sf_id_up.std())
            print("sf_id_down: ", sf_id_down.mean(), "+-", sf_id_down.std())
            print("sf_iso_up: ", sf_iso_up.mean(), "+-", sf_iso_up.std())
            print("sf_iso_down: ", sf_iso_down.mean(), "+-", sf_iso_down.std())
            print("sf_trig_up: ", sf_trig_up.mean(), "+-", sf_trig_up.std())
            print("sf_trig_down: ", sf_trig_down.mean(), "+-", sf_trig_down.std())

        sfs_id += [sf_id]
        sfs_iso += [sf_iso]
        sfs_trig += [sf_trig]

        sfs_id_up += [sf_id_up]
        sfs_id_down += [sf_id_down]
        sfs_iso_up += [sf_iso_up]
        sfs_iso_down += [sf_iso_down]
        sfs_trig_up += [sf_trig_up]
        sfs_trig_down += [sf_trig_down]

    #multiply all ID, iso, trigger weights for leading and subleading muons
    sf_id = multiply_all(sfs_id)
    sf_iso = multiply_all(sfs_iso)
    sf_trig = multiply_all(sfs_trig)
    sf_id_up = multiply_all(sfs_id_up)
    sf_id_down = multiply_all(sfs_id_down)
    sf_iso_up = multiply_all(sfs_iso_up)
    sf_iso_down = multiply_all(sfs_iso_down)
    sf_trig_up = multiply_all(sfs_trig_up)
    sf_trig_down = multiply_all(sfs_trig_down)

    #move to GPU
    if use_cuda:
        sf_tot = NUMPY_LIB.array(sf_tot)

    return {
        "id": sf_id,
        "iso": sf_iso,
        "trigger": sf_trig,
        "id__up": sf_id_up,
        "id__down": sf_id_down,
        "iso__up": sf_iso_up,
        "iso__down": sf_iso_down,
        "trigger__up": sf_trig_up,
        "trigger__down": sf_trig_down
    }

def jaggedstruct_print(struct, idx, attrs):
    of1 = struct.offsets[idx]
    of2 = struct.offsets[idx+1]
    print("nstruct", of2-of1)
    for i in range(of1, of2):
        print("s", [getattr(struct, a)[i] for a in attrs])

def deepdive_event(scalars, mask_events, ret_mu, jets, muons, id):
    print("deepdive")
    idx = np.where(scalars["event"]==id)[0][0]
    print("scalars:", {k: v[idx] for k, v in scalars.items()})
    print("trigger:", mask_events[idx])
    print("muon:", ret_mu["selected_events"][idx])
    jaggedstruct_print(jets, idx, ["pt", "eta"])
    jaggedstruct_print(muons, idx, ["pt", "eta", "mediumId", "pfRelIso04_all", "charge", "triggermatch", "passes_leading_pt", "pass_id", "pass_os"])

def sync_printout(
    ret_mu, muons, scalars,
    leading_muon, subleading_muon, inv_mass,
    n_additional_muons, n_additional_electrons,
    ret_jet, leading_jet, subleading_jet):
    with open("log_sync.txt", "w") as fi:
        msk = ret_mu["selected_events"] & (
            NUMPY_LIB.logical_or(
                (inv_mass > 110.0) & (inv_mass < 150.0),
                (inv_mass > 76.0) & (inv_mass < 106.0)
            )
        )
        for iev in range(muons.numevents()):
            if msk[iev]:
                s = ""
                s += "{0} ".format(scalars["run"][iev])
                s += "{0} ".format(scalars["luminosityBlock"][iev])
                s += "{0} ".format(scalars["event"][iev])
                s += "{0} ".format(ret_jet["num_jets"][iev])
                s += "{0} ".format(ret_jet["num_jets_btag"][iev])
                s += "{0} ".format(n_additional_muons[iev])
                s += "{0} ".format(n_additional_electrons[iev])
                s += "{0:.2f} ".format(leading_muon["pt"][iev])
                s += "{0:.2f} ".format(leading_muon["eta"][iev])
                s += "{0:.2f} ".format(subleading_muon["pt"][iev])
                s += "{0:.2f} ".format(subleading_muon["eta"][iev])
                s += "{0:.2f} ".format(inv_mass[iev])

                s += "{0:.2f} ".format(leading_jet["pt"][iev])
                s += "{0:.2f} ".format(leading_jet["eta"][iev])
                s += "{0:.2f} ".format(subleading_jet["pt"][iev])
                s += "{0:.2f} ".format(subleading_jet["eta"][iev])

                s += "{0:.2f} ".format(ret_jet["dijet_inv_mass"][iev])

                #category index
                s += "{0} ".format(scalars["category"][iev])
                print(s, file=fi)

def assign_category(
    njet, nbjet, n_additional_muons, n_additional_electrons,
    dijet_inv_mass, leading_jet, subleading_jet, cat5_dijet_inv_mass_cut, cat5_abs_jj_deta_cut):
    cats = NUMPY_LIB.zeros_like(njet)
    cats[:] = -9999

    msk_prev = NUMPY_LIB.zeros_like(cats, dtype=NUMPY_LIB.bool)

    jj_deta = NUMPY_LIB.abs(leading_jet["eta"] - subleading_jet["eta"])

    #cat 1, ttH
    msk_1 = (nbjet > 0) & NUMPY_LIB.logical_or(n_additional_muons > 0, n_additional_electrons > 0)
    cats[NUMPY_LIB.invert(msk_prev) & msk_1] = 1
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_1)

    #cat 2
    msk_2 = (nbjet > 0) & (njet > 1)
    cats[NUMPY_LIB.invert(msk_prev) & msk_2] = 2
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_2)

    #cat 3
    msk_3 = (n_additional_muons > 0)
    cats[NUMPY_LIB.invert(msk_prev) & msk_3] = 3
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_3)

    #cat 4
    msk_4 = (n_additional_electrons > 0)
    cats[NUMPY_LIB.invert(msk_prev) & msk_4] = 4
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_4)

    #cat 5
    msk_5 = (dijet_inv_mass > cat5_dijet_inv_mass_cut) & (jj_deta > cat5_abs_jj_deta_cut)
    cats[NUMPY_LIB.invert(msk_prev) & msk_5] = 5
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_5)

    #cat 6
    msk_6 = (dijet_inv_mass > 70) & (dijet_inv_mass < 100)
    cats[NUMPY_LIB.invert(msk_prev) & msk_6] = 6
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_6)

    #cat 7
    msk_7 = (njet > 1)
    cats[NUMPY_LIB.invert(msk_prev) & msk_7] = 7
    msk_prev = NUMPY_LIB.logical_or(msk_prev, msk_7)

    cats[NUMPY_LIB.invert(msk_prev)] = 8

    return cats

def check_and_fix_qgl(jets):
    msk = NUMPY_LIB.isnan(jets["qgl"])
    jets["qgl"][msk] = -1
    if debug:
        if NUMPY_LIB.sum(msk) > 0:
            print("jets with qgl = NaN")
            print("pt", jets["pt"][msk])
            print("eta", jets["eta"][msk])
            print("puId", jets["puId"][msk])

def get_numev_passed(nev, masks):
    out = Results({})
    out["all"] = nev
    for name, mask in masks.items():
        out[name] = float(NUMPY_LIB.sum(mask))
    return out
 
def load_puhist_target(filename):
    fi = uproot.open(filename)
    
    h = fi["pileup"]
    edges = np.array(h.edges)
    values_nominal = np.array(h.values)
    values_nominal = values_nominal / np.sum(values_nominal)
    
    h = fi["pileup_plus"]
    values_up = np.array(h.values)
    values_up = values_up / np.sum(values_up)
    
    h = fi["pileup_minus"]
    values_down = np.array(h.values)
    values_down = values_down / np.sum(values_down)
    return edges, (values_nominal, values_up, values_down)

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]

def cache_data(job_descriptions, parameters, cmdline_args):
    if cmdline_args.nthreads == 1:
        tot_ev = 0
        tot_mb = 0
        for result in map(cache_data_multiproc_worker, [
            (job_desc, parameters, cmdline_args) for job_desc in job_descriptions]):
            tot_ev += result[0]
            tot_mb += result[1]
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=cmdline_args.nthreads) as executor:
            tot_ev = 0
            tot_mb = 0
            for result in executor.map(cache_data_multiproc_worker, [
                (job_desc, parameters, cmdline_args) for job_desc in job_descriptions]):
                tot_ev += result[0]
                tot_mb += result[1]
            print("waiting for completion")
    return tot_ev, tot_mb

"""Given a ROOT file, run any checks that can only be done
on the original file. In our case, we need to access the number
of generated events.
"""
def func_filename_precompute_mc(filename):
    sumw, sumw2 = get_gen_sumweights([filename])
    ret = {"genEventSumw": sumw, "genEventSumw2": sumw2}
    return ret
 
def create_dataset(name, filenames, datastructures, cache_location, datapath, is_mc):
    ds = Dataset(name, filenames, datastructures, cache_location=cache_location, datapath=datapath, treename="Events", is_mc=is_mc)
    if is_mc:
        ds.func_filename_precompute = func_filename_precompute_mc
    return ds

def cache_preselection(ds, hlt_bits):
    for ifile in range(len(ds.filenames)):

        #OR of the trigger bits by summing
        hlt_res = [ds.eventvars[ifile][hlt_bit]==1 for hlt_bit in hlt_bits]
        sel = NUMPY_LIB.stack(hlt_res).sum(axis=0) >= 1

        #If we didn't have >=2 muons in NanoAOD, no need to keep this event 
        sel = sel & (ds.eventvars[ifile]["nMuon"] >= 2)

        for structname in ds.structs.keys():
            struct_compact = ds.structs[structname][ifile].compact_struct(sel)
            ds.structs[structname][ifile] = struct_compact
        for evvar_name in ds.eventvars[ifile].keys():
            ds.eventvars[ifile][evvar_name] = ds.eventvars[ifile][evvar_name][sel]

def cache_data_multiproc_worker(args):
    job_desc, parameters, cmdline_args = args

    print("verifying cache for {0}".format(job_desc))
    filenames_all = job_desc["filenames"]
    assert(len(filenames_all)==1)
    filename = filenames_all[0]

    dataset_name = job_desc["dataset_name"]
    dataset_era = job_desc["dataset_era"]
    is_mc = job_desc["is_mc"]

    datastructure = create_datastructure(dataset_name, is_mc, dataset_era)

    #Used for preselection in the cache
    hlt_bits = parameters["baseline"]["hlt_bits"][dataset_era]
    t0 = time.time()
    ds = create_dataset(dataset_name, filenames_all, datastructure, cmdline_args.cache_location, cmdline_args.datapath, is_mc)
    ds.numpy_lib = np

    #Skip loading this file if cache already done
    if ds.check_cache():
        print("Cache on file {0} is complete, skipping".format(filename))
        return 0, 0

    ds.load_root()

    #put any preselection here
    processed_size_mb = ds.memsize()/1024.0/1024.0
    cache_preselection(ds, hlt_bits)
    processed_size_mb_post = ds.memsize()/1024.0/1024.0

    ds.to_cache()
    t1 = time.time()
    dt = t1 - t0
    print("built cache for {0}, loaded {1:.2f} MB, cached {2:.2f} MB, {3:.2E} Hz, {4:.2f} MB/s".format(
        filename, processed_size_mb, processed_size_mb_post, len(ds)/dt, processed_size_mb/dt))
    return len(ds), processed_size_mb

#Branches to load from the ROOT files
def create_datastructure(dataset_name, is_mc, dataset_era):
    datastructures = {
        "Muon": [
            ("Muon_pt", "float32"), ("Muon_eta", "float32"),
            ("Muon_phi", "float32"), ("Muon_mass", "float32"),
            ("Muon_pdgId", "int32"),
            ("Muon_pfRelIso04_all", "float32"), ("Muon_mediumId", "bool"),
            ("Muon_tightId", "bool"), ("Muon_charge", "int32"),
            ("Muon_isGlobal", "bool"), ("Muon_isTracker", "bool"),
            ("Muon_nTrackerLayers", "int32"), ("Muon_ptErr", "float32"),
        ],
        "Electron": [
            ("Electron_pt", "float32"), ("Electron_eta", "float32"),
            ("Electron_phi", "float32"), ("Electron_mass", "float32"),
            ("Electron_pfRelIso03_all", "float32"),
            ("Electron_mvaFall17V1Iso_WP90", "bool"),
        ],
        "Jet": [
            ("Jet_pt", "float32"),
            ("Jet_eta", "float32"),
            ("Jet_phi", "float32"),
            ("Jet_mass", "float32"),
            ("Jet_btagDeepB", "float32"),
            ("Jet_qgl", "float32"),
            ("Jet_jetId", "int32"),
            ("Jet_puId", "int32"),
            ("Jet_area", "float32"),
            ("Jet_rawFactor", "float32")
        ],
        "SoftActivityJet": [
            ("SoftActivityJet_pt", "float32"),
            ("SoftActivityJet_eta", "float32"),
            ("SoftActivityJet_phi", "float32"),
        ],
        "TrigObj": [
            ("TrigObj_pt", "float32"),
            ("TrigObj_eta", "float32"),
            ("TrigObj_phi", "float32"),
            ("TrigObj_id", "int32")
        ],
        "EventVariables": [
            ("nMuon", "int32"),
            ("PV_npvsGood", "float32"), 
            ("PV_ndof", "float32"),
            ("PV_z", "float32"),
            ("Flag_BadChargedCandidateFilter", "bool"),
            ("Flag_HBHENoiseFilter", "bool"),
            ("Flag_HBHENoiseIsoFilter", "bool"),
            ("Flag_EcalDeadCellTriggerPrimitiveFilter", "bool"),
            ("Flag_goodVertices", "bool"),
            ("Flag_globalSuperTightHalo2016Filter", "bool"),
            ("Flag_BadPFMuonFilter", "bool"),
            ("Flag_BadChargedCandidateFilter", "bool"),
            ("run", "uint32"),
            ("luminosityBlock", "uint32"),
            ("event", "uint64"),
            #("SoftActivityJetNjets5", "int32"),
            ("fixedGridRhoFastjetAll", "float32"),
        ],
    }

    if is_mc:
        datastructures["EventVariables"] += [
            ("Pileup_nTrueInt", "uint32"),
            ("Generator_weight", "float32"),
            ("genWeight", "float32")
        ]
        if dataset_era == "2016" or dataset_era == "2017":
            datastructures["EventVariables"] += [
                ("L1PreFiringWeight_Nom", "float32"),
                ("L1PreFiringWeight_Dn", "float32"),
                ("L1PreFiringWeight_Up", "float32")
            ]
        datastructures["Muon"] += [
            ("Muon_genPartIdx", "int32"),
        ]
        datastructures["GenPart"] = [
            ("GenPart_pt", "float32"),
            ("GenPart_eta", "float32"),
            ("GenPart_phi", "float32"),
            ("GenPart_pdgId", "int32"),
            ("GenPart_status", "int32"),
        ]
        if "psweight" in dataset_name:
            datastructures["psweight"] = [
                ("PSWeight", "float32"),
            ]
        if "dy" in dataset_name or "ewk" in dataset_name:
            datastructures["LHEPdfWeight"] = [
                ("LHEPdfWeight", "float32"),
            ]
            datastructures["LHEScaleWeight"] = [
                ("LHEScaleWeight", "float32"),
            ]
        datastructures["Jet"] += [
            ("Jet_genJetIdx", "int32")
        ]
        datastructures["GenJet"] = [
            ("GenJet_pt", "float32"), 
            ("GenJet_eta", "float32"), 
            ("GenJet_phi", "float32"), 
            ("GenJet_mass", "float32"), 
        ]

    if dataset_era == "2016":
        datastructures["EventVariables"] += [
            ("HLT_IsoMu24", "bool"),
            ("HLT_IsoTkMu24", "bool"),
            ("MET_pt", "float32"),
        ]
    elif dataset_era == "2017":
        datastructures["EventVariables"] += [
            ("HLT_IsoMu27", "bool"),
            ("METFixEE2017_pt", "float32"),
        ]
    elif dataset_era == "2018":
        datastructures["EventVariables"] += [
            ("HLT_IsoMu24", "bool"),
            ("MET_pt", "float32"),
        ]

    return datastructures

###
### Threading stuff
###

def threaded_batches_feeder(tokill, batches_queue, dataset_generator):
    while not tokill():
        ds = dataset_generator.nextone()
        if ds is None:
            break 
        batches_queue.put(ds, block=True)
    #print("Cleaning up threaded_batches_feeder worker", threading.get_ident())
    return

class thread_killer(object):
    """Boolean object for signaling a worker thread to terminate
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.to_kill = False
    
    def __call__(self):
        return self.to_kill
    
    def set_tokill(self,tokill):
        with self.lock:
            self.to_kill = tokill

class InputGen:
    def __init__(self, job_descriptions, cache_location, datapath):

        self.job_descriptions = job_descriptions
        self.chunk_lock = threading.Lock()
        self.loaded_lock = threading.Lock()
        self.num_chunk = 0
        self.num_loaded = 0

        self.cache_location = cache_location
        self.datapath = datapath

    def is_done(self):
        return (self.num_chunk == len(self)) and (self.num_loaded == len(self))

    #did not make this a generator to simplify handling the thread locks
    def nextone(self):
        self.chunk_lock.acquire()

        if self.num_chunk > 0 and self.num_chunk == len(self):
            self.chunk_lock.release()
            print("Generator is done: num_chunk={0}, len(self.job_descriptions)={1}".format(self.num_chunk, len(self)))
            return None

        job_desc = self.job_descriptions[self.num_chunk]
        print("Loading dataset {0} job desc {1}/{2}, {3}".format(
            job_desc["dataset_name"], self.num_chunk, len(self.job_descriptions), job_desc["filenames"]))

        datastructures = create_datastructure(job_desc["dataset_name"], job_desc["is_mc"], job_desc["dataset_era"])

        ds = create_dataset(
            job_desc["dataset_name"],
            job_desc["filenames"],
            datastructures,
            self.cache_location,
            self.datapath,
            job_desc["is_mc"])

        ds.era = job_desc["dataset_era"]
        ds.numpy_lib = numpy
        ds.num_chunk = job_desc["dataset_num_chunk"]
        self.num_chunk += 1
        self.chunk_lock.release()

        # Load caches on multiple threads
        ds.from_cache()

        #Merge data arrays to one big array
        ds.merge_inplace()

        # Increment the counter for number of loaded datasets
        with self.loaded_lock:
            self.num_loaded += 1

        return ds

    def __call__(self):
        return self.__iter__()

    def __len__(self):
        return len(self.job_descriptions)


def create_dataset_jobfiles(
    dataset_name, dataset_era,
    filenames, is_mc, chunksize, outpath):
    try:
        os.makedirs(outpath + "/jobfiles")
    except Exception as e:
        pass

    job_descriptions = []
    ijob = 0
    for files_chunk in chunks(filenames, chunksize):
        job_description = {
            "dataset_name": dataset_name,
            "dataset_era": dataset_era,
            "filenames": files_chunk,
            "is_mc": is_mc,
            "dataset_num_chunk": ijob,
        }
        job_descriptions += [job_description]
        fn = outpath + "/jobfiles/{0}_{1}_{2}.json".format(dataset_name, dataset_era, ijob)
        if os.path.isfile(fn):
            if ijob == 0:
                print("Jobfile {0} exists, not recreating this one or others for this dataset".format(fn))
                print("Delete the folder {0}/jobfiles if you would like the files to be recreated".format(outpath))
                print("You might want this when you changed the --chunksize option.")
        else:
            with open(fn, "w") as fi:
                fi.write(json.dumps(job_description, indent=2))

        ijob += 1
    return job_descriptions


###
### Functions not currently used
###

# def significance_templates(sig_samples, bkg_samples, rets, analysis, histogram_names, do_plots=False, ntoys=1):
     
#     Zs = []
#     Zs_naive = []
#     if do_plots:
#         for k in histogram_names:
#             plt.figure(figsize=(4,4))
#             ax = plt.axes()
#             plt.title(k)
#             for samp in sig_samples:
#                 h = rets[samp][analysis][k]["puWeight"]
#                 plot_hist_step(ax, h.edges, 100*h.contents, 100*np.sqrt(h.contents_w2), kwargs_step={"label":samp})
#             for samp in bkg_samples:
#                 h = rets[samp][analysis][k]["puWeight"]
#                 plot_hist_step(ax, h.edges, h.contents, np.sqrt(h.contents_w2), kwargs_step={"label":samp})

#     #         for name in ["ggh", "tth", "vbf", "wmh", "wph", "zh"]:
#     #             plot_hist(100*rets[name][analysis][k]["puWeight"], label="{0} ({1:.2E})".format(name, np.sum(rets[name][k]["puWeight"].contents)))
#     #         plot_hist(rets["dy"][k]["puWeight"], color="black", marker="o",
#     #             label="DY ({0:.2E})".format(np.sum(rets["dy"][k]["puWeight"].contents)), linewidth=0, elinewidth=1
#     #         )
#             plt.legend(frameon=False, ncol=2)
#             ymin, ymax = ax.get_ylim()
#             ax.set_ylim(ymin, 5*ymax)
    
#     for i in range(ntoys):
#         arrs_sig = []
#         arrs_bkg = []
#         for hn in histogram_names:
#             arrs = {}
#             for samp in sig_samples + bkg_samples:
#                 if ntoys == 1:
#                     arrs[samp] = rets[samp][analysis][hn]["puWeight"].contents,
#                 else:
#                     arrs[samp] = np.random.normal(
#                         rets[samp][analysis][hn]["puWeight"].contents,
#                         np.sqrt(rets[samp][analysis][hn]["puWeight"].contents_w2)
#                     )
        
#             arr_sig = np.sum([arrs[s] for s in sig_samples])
#             arr_bkg = np.sum([arrs[s] for s in bkg_samples])
#             arrs_sig += [arr_sig]
#             arrs_bkg += [arr_bkg]
        
#         arr_sig = np.hstack(arrs_sig)
#         arr_bkg = np.hstack(arrs_bkg)

#         Z = sig_q0_asimov(arr_sig, arr_bkg)
#         Zs += [Z]

#         Znaive = sig_naive(arr_sig, arr_bkg)
#         Zs_naive += [Znaive]
#     return (np.mean(Zs), np.std(Zs)), (np.mean(Zs_naive), np.std(Zs_naive))

# def compute_significances(sig_samples, bkg_samples, r, analyses):
#     Zs = []
#     for an in analyses:
#         templates = [c for c in r["ggh"][an].keys() if "__cat" in c and c.endswith("__inv_mass")]
#         (Z, eZ), (Zc, eZc) = significance_templates(
#             sig_samples, bkg_samples, r, an, templates, ntoys=1
#         )
#         Zs += [(an, Z)]
#     return sorted(Zs, key=lambda x: x[1], reverse=True)

# def load_analysis(mc_samples, outpath, cross_sections, cat_trees):
#     res = {}
#     #res["data"] = json.load(open("../out/data_2017.json"))
#     #lumi = res["data"]["baseline"]["int_lumi"]
#     lumi = 41000.0

#     rets = {
#         k: json.load(open("{0}/{1}.json".format(outpath, k))) for k in mc_samples
#     }

#     histograms = {}
#     for name in mc_samples:
#         ret = rets[name]
#         histograms[name] = {}
#         for analysis in cat_trees:
#             ret_an = rets[name]["baseline"][analysis]
#             histograms[name][analysis] = {}
#             for kn in ret_an.keys():
#                 if kn.startswith("hist_"):
#                     histograms[name][analysis][kn] = {}
#                     for w in ret_an[kn].keys():
#                         h = (1.0 / ret["gen_sumweights"]) * lumi * cross_sections[name] * Histogram.from_dict(ret_an[kn][w])
#                         h.label = "{0} ({1:.1E})".format(name, np.sum(h.contents))
#                         histograms[name][analysis][kn][w] = h

#     return histograms

# def optimize_categories(sig_samples, bkg_samples, varlist, datasets, lumidata, lumimask, pu_corrections_2017, cross_sections, args, analysis_parameters, best_tree):
#     Zprev = 0
#     #Run optimization
#     for num_iter in range(args.niter):
#         outpath = "{0}/iter_{1}".format(args.out, num_iter)

#         try:
#             os.makedirs(outpath)
#         except FileExistsError as e:
#             pass

#         analysis_parameters["baseline"]["categorization_trees"] = {}
#         #analysis_parameters["baseline"]["categorization_trees"] = {"varA": copy.deepcopy(varA), "varB": copy.deepcopy(varB)}
#         analysis_parameters["baseline"]["categorization_trees"]["previous_best"] = copy.deepcopy(best_tree)

#         cut_trees = generate_cut_trees(100, varlist, best_tree)
#         for icut, dt in enumerate(cut_trees):
#             an_name = "an_cuts_{0}".format(icut)
#             analysis_parameters["baseline"]["categorization_trees"][an_name] = dt

#         with open('{0}/parameters.pickle'.format(outpath), 'wb') as handle:
#             pickle.dump(analysis_parameters, handle, protocol=pickle.HIGHEST_PROTOCOL)

#         run_analysis(args, outpath, datasets, analysis_parameters, lumidata, lumimask, pu_corrections)
#         cut_trees = sorted(list(analysis_parameters["baseline"]["categorization_trees"].keys()), reverse=True)
#         r = load_analysis(sig_samples + bkg_samples, outpath, cross_sections, cut_trees)
#         print("computing expected significances")
#         Zs = compute_significances(sig_samples, bkg_samples, r, cut_trees)

#         with open('{0}/sigs.pickle'.format(outpath), 'wb') as handle:
#             pickle.dump(Zs, handle, protocol=pickle.HIGHEST_PROTOCOL)
        
#         print("optimization", num_iter, Zs[:10], Zprev)
#         best_tree = copy.deepcopy(analysis_parameters["baseline"]["categorization_trees"][Zs[0][0]])
#         Zprev = Zs[0][1]

#     return best_tree

def parse_nvidia_smi():
    """Returns the GPU symmetric multiprocessor and memory usage in %
    """
    try:
        import nvidia_smi
        nvidia_smi.nvmlInit()
        handle = nvidia_smi.nvmlDeviceGetHandleByIndex(0)
        res = nvidia_smi.nvmlDeviceGetUtilizationRates(handle)
        return {"gpu": res.gpu, "mem": res.memory}
    except Exception as e:
        return {"gpu": 0, "mem": 0}

def threaded_metrics(tokill, train_batches_queue):
    global global_metrics
    c = psutil.disk_io_counters()
    bytes_read_start = c.read_bytes
    thisproc = psutil.Process()

    while not tokill(): 
        dt = 1.0
        
        c = psutil.disk_io_counters()

        bytes_read_speed = (c.read_bytes - bytes_read_start)/dt/1024.0/1024.0
        bytes_read_start = c.read_bytes

        cpu_pct = thisproc.cpu_percent()
        cpu_times = thisproc.cpu_times()
        memory_info = thisproc.memory_info()
        d = parse_nvidia_smi()

        metrics_dict = {
            "disk_io": bytes_read_speed,
            "cpu_percent": cpu_pct,
            #"cpu_iowait=": cpu_times.iowait,
            "rss": memory_info.rss/1024.0/1024.0,
            "gpu_util": d["gpu"],
            "gpu_mem": d["mem"],
            "queue_size": train_batches_queue.qsize(),
        }
        global_metrics += [metrics_dict]
        time.sleep(dt)
    print("threaded_metrics done")
    return
