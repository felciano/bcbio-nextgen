"""Provide validation of structural variations against truth sets.

Tests overlaps of the combined ensemble structural variant BED against
a set of known regions.  Requires any overlap between ensemble set and
known regions, and removes regions from analysis that overlap with
exclusion regions.
"""
import collections
import csv
import os

import toolz as tz
import numpy as np
import pandas as pd
try:
    import matplotlib as mpl
    mpl.use('Agg', force=True)
    import matplotlib.pyplot as plt
except ImportError:
    mpl, plt = None, None
try:
    import seaborn as sns
except ImportError:
    sns = None

from bcbio.log import logger
from bcbio import utils
from bcbio.pipeline import datadict as dd

EVENT_SIZES = [(1, 450), (450, 2000), (2000, 4000), (4000, 20000), (20000, 60000),
               (60000, int(1e6))]

def _stat_str(x, n):
    if n > 0:
        val = float(x) / float(n) * 100.0
        return {"label": "%.1f%% (%s / %s)" % (val, x, n), "val": val}
    else:
        return {"label": "", "val": 0}

def cnv_to_event(name, data):
    """Convert a CNV to an event name.
    """
    ploidy = dd.get_ploidy(data)
    if name.startswith("cnv"):
        num = max([int(x) for x in name.split("_")[0].replace("cnv", "").split(";")])
        if num < ploidy:
            return "DEL"
        elif num > ploidy:
            return "DUP"
        else:
            return name
    else:
        return name

def callers_by_event(in_bed, data):
    """Retrieve callers with calls for each event type.
    """
    total_callers = collections.defaultdict(set)
    with open(in_bed) as in_handle:
        for line in in_handle:
            caller_strs = line.strip().split()[3]
            for event, caller in [x.split("_", 1) for x in caller_strs.split(",")]:
                total_callers[cnv_to_event(event, data)].add(caller)
    return total_callers

def _evaluate_one(caller, svtype, size_range, ensemble, truth, data):
    """Compare a ensemble results for a caller against a specific caller and SV type.
    """
    import pybedtools
    def cnv_matches(name):
        return cnv_to_event(name, data) == svtype
    def wham_matches(name):
        """Flexibly handle WHAM comparisons, allowing DUP/DEL matches during comparisons.
        """
        allowed = {"DEL": set(["DEL", "UKN"]),
                   "DUP": set(["DUP"]),
                   "INV": set(["INV"])}
        curtype, curcaller = name.split("_")[:2]
        return curcaller == "wham" and svtype in allowed and curtype in allowed[svtype]
    def is_breakend(name):
        return name.startswith("BND")
    def in_size_range(feat):
        minf, maxf = size_range
        size = feat.end - feat.start
        return size >= minf and size < maxf
    def is_caller_svtype(feat):
        for name in feat.name.split(","):
            if ((name.startswith(svtype) or cnv_matches(name) or wham_matches(name) or is_breakend(name))
                  and (caller == "sv-ensemble" or name.endswith(caller))):
                return True
        return False
    minf, maxf = size_range
    if minf < 600:
        overlap = 0.2
    elif minf < 2500:
        overlap = 0.5
    else:
        overlap = 0.8
    efeats = pybedtools.BedTool(ensemble).filter(in_size_range).filter(is_caller_svtype).saveas().sort().merge()
    tfeats = pybedtools.BedTool(truth).filter(in_size_range).sort().merge().saveas()
    etotal = efeats.count()
    ttotal = tfeats.count()
    match = efeats.intersect(tfeats, u=True).sort().merge().saveas().count()
    return {"sensitivity": _stat_str(match, ttotal),
            "precision": _stat_str(match, etotal)}

def _evaluate_multi(callers, truth_svtypes, ensemble, call_beds, data):
    out_file = "%s-validate.csv" % utils.splitext_plus(ensemble)[0]
    df_file = "%s-validate-df.csv" % utils.splitext_plus(ensemble)[0]
    if not utils.file_uptodate(out_file, ensemble) or not utils.file_uptodate(df_file, ensemble):
        with open(out_file, "w") as out_handle:
            with open(df_file, "w") as df_out_handle:
                writer = csv.writer(out_handle)
                dfwriter = csv.writer(df_out_handle)
                total_callers = callers_by_event(ensemble, data)
                writer.writerow(["svtype", "size", "caller", "sensitivity", "precision"])
                dfwriter.writerow(["svtype", "size", "caller", "metric", "value", "label"])
                for svtype, truth in truth_svtypes.items():
                    for size in EVENT_SIZES:
                        str_size = "%s-%s" % size
                        for caller in (x for x in callers if x in total_callers[svtype] or x == "sv-ensemble"):
                            try:
                                call_bed = call_beds[caller]
                            except KeyError:
                                assert caller == "sv-ensemble", caller
                                call_bed = ensemble
                            evalout = _evaluate_one(caller, svtype, size, call_bed, truth, data)
                            writer.writerow([svtype, str_size, caller,
                                             evalout["sensitivity"]["label"], evalout["precision"]["label"]])
                            for metric in ["sensitivity", "precision"]:
                                dfwriter.writerow([svtype, str_size, caller, metric,
                                                   evalout[metric]["val"], evalout[metric]["label"]])
    return out_file, df_file

def _plot_evaluation(df_csv):
    if mpl is None or plt is None or sns is None:
        not_found = ", ".join([x for x in ['mpl', 'plt', 'sns'] if eval(x) is None])
        logger.info("No validation plot. Missing imports: %s" % not_found)
        return None
    df = pd.read_csv(df_csv).fillna("0%")
    out = {}
    for event in df["svtype"].unique():
        out[event] = _plot_evaluation_event(df_csv, event)
    return out

def _plot_evaluation_event(df_csv, svtype):
    """Provide plot of evaluation metrics for an SV event, stratified by event size.
    """
    titles = {"INV": "Inversions", "DEL": "Deletions", "DUP": "Duplications",
              "INS": "Insertions"}
    out_file = "%s-%s.png" % (os.path.splitext(df_csv)[0], svtype)
    sns.set(style='white')
    if not utils.file_uptodate(out_file, df_csv):
        metrics = ["sensitivity", "precision"]
        df = pd.read_csv(df_csv).fillna("0%")
        df = df[(df["svtype"] == svtype)]
        event_sizes = _find_events_to_include(df, EVENT_SIZES)
        fig, axs = plt.subplots(len(event_sizes), len(metrics), tight_layout=True)
        if len(event_sizes) == 1:
            axs = [axs]
        callers = sorted(df["caller"].unique())
        if "sv-ensemble" in callers:
            callers.remove("sv-ensemble")
            callers.append("sv-ensemble")
        for i, size in enumerate(event_sizes):
            size_label = "%s to %sbp" % size
            size = "%s-%s" % size
            for j, metric in enumerate(metrics):
                ax = axs[i][j]
                ax.get_xaxis().set_ticks([])
                ax.spines['bottom'].set_visible(False)
                ax.spines['left'].set_visible(False)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                ax.set_xlim(0, 125.0)
                if i == 0:
                    ax.set_title(metric, size=12, y=1.2)
                vals, labels = _get_plot_val_labels(df, size, metric, callers)
                ax.barh(np.arange(len(vals)), vals)
                if j == 0:
                    ax.tick_params(axis='y', which='major', labelsize=8)
                    ax.locator_params(nbins=len(callers) + 2, axis="y", tight=True)
                    ax.set_yticklabels(callers, va="bottom")
                    ax.text(100, len(callers), size_label, fontsize=10)
                else:
                    ax.get_yaxis().set_ticks([])
                for ai, (val, label) in enumerate(zip(vals, labels)):
                    ax.annotate(label, (val + 0.75, ai + 0.35), va='center', size=7)
        if svtype in titles:
            fig.text(0.025, 0.95, titles[svtype], size=14)
        fig.set_size_inches(7, len(event_sizes) + 1)
        fig.savefig(out_file)
    return out_file

def _find_events_to_include(df, event_sizes):
    out = []
    for size in event_sizes:
        str_size = "%s-%s" % size
        curdf = df[(df["size"] == str_size) & (df["metric"] == "sensitivity")]
        for val in list(curdf["label"]):
            if val != "0%":
                out.append(size)
                break
    return out

def _get_plot_val_labels(df, size, metric, callers):
    curdf = df[(df["size"] == size) & (df["metric"] == metric)]
    vals, labels = [], []
    for caller in callers:
        row = curdf[curdf["caller"] == caller]
        val = list(row["value"])[0]
        if val == 0:
            val = 0.1
        vals.append(val)
        labels.append(list(row["label"])[0])
    return vals, labels

def evaluate(data, sv_calls):
    """Provide evaluations for multiple callers split by structural variant type.
    """
    truth_sets = tz.get_in(["config", "algorithm", "svvalidate"], data)
    ensemble_callsets = [(i, x) for (i, x) in enumerate(sv_calls) if x["variantcaller"] == "sv-ensemble"]
    if truth_sets and len(ensemble_callsets) > 0:
        ensemble = ensemble_callsets[0][-1]
        call_beds = {c: f for (c, f) in ensemble["input_beds"]}
        ensemble_bed = ensemble["vrn_file"]
        callers = ["sv-ensemble"] + sorted(call_beds.keys())
        val_summary, df_csv = _evaluate_multi(callers, truth_sets, ensemble_bed, call_beds, data)
        summary_plots = _plot_evaluation(df_csv)
        ensemble_i, ensemble = ensemble_callsets[0]
        ensemble["validate"] = {"csv": val_summary, "plot": summary_plots, "df": df_csv}
        sv_calls[ensemble_i] = ensemble
    return sv_calls

if __name__ == "__main__":
    #_, df_csv = _evaluate_multi(["lumpy", "delly", "wham", "sv-ensemble"],
    #                            {"DEL": "synthetic_challenge_set3_tumor_20pctmasked_truth_sv_DEL.bed"},
    #                            "syn3-tumor-ensemble-filter.bed", "sv_exclude.bed")
    #_, df_csv = _evaluate_multi(["lumpy", "delly", "cn_mops", "sv-ensemble"],
    #                            {"DEL": "NA12878.50X.ldgp.molpb_val.20140508.bed"},
    #                            "NA12878-ensemble.bed", "LCR.bed.gz")
    import sys
    _plot_evaluation(sys.argv[1])
