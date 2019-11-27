from os.path import join, isdir
from os import listdir
import argparse
import json

import matplotlib.pyplot as plt

SCORES = ['mean_F1', 'IC', 'IB', 'VC', 'VB', 'mean_OR', 'mean_OL']
BASELINES = [0.47369345142021646, 0.4257722385427191, 116, 0.5372817904136711,
             24, 0.34630964300920797, 0.45358382820115767]
FILTERS = ["log_prob_ratio", "log_prob_sum", "log_prob"]


def compare(args):
    for i, score in enumerate(SCORES):
        baseline = BASELINES[i]
        compare_score(args, score, baseline)


def compare_score(args, score_name='mean_F1', baseline=0.47369345142021646):

    assert all(args.percentiles[i] <= args.percentiles[i+1]
               for i in range(len(args.percentiles)-1)), 'percentiles must be sorted'

    # Add one list for each filtering criteria
    criteria_scores = {'baseline': []}
    for criteria in args.criteria:
        criteria_scores[criteria] = []

    # Append values from json to each list
    if args.action == "track_vis":
        for curv in args.max_curv:
            scoring_dir = join(args.results_path, f"scorings_trackvis_c-{curv}")
            if not isdir(scoring_dir):
                raise FileNotFoundError(f'File {scoring_dir} does not exist!')

            scoring_dir = join(scoring_dir, "scores")
            json_path = [file for file in listdir(scoring_dir)
                         if file.endswith('.json')]

            # Un comment for local use!
            # json_path = join(args.results_path, f'trackvis_{curv}.json')

            with open(json_path) as json_file:
                scores = json.load(json_file)

            criteria_scores[criteria].append(scores[score_name])
        criteria_scores['baseline'].append(baseline)

    else:
        for percentile in args.percentiles:
            for criteria in args.criteria:
                scoring_dir = join(args.results_path,
                                   f"scorings_p_{percentile}-f_{criteria}_{args.action}")
                if not isdir(scoring_dir):
                    raise FileNotFoundError(f'File {scoring_dir} does not exist!')

                scoring_dir = join(scoring_dir, "scores")
                json_path = [file for file in listdir(scoring_dir)
                             if file.endswith('.json')]

                # Un comment for local use!
                # json_path = join(args.results_path, f'{criteria}_{percentile}_fib_k=f.json')
                # json_path = join(args.results_path, f'{criteria}_{percentile}_bund.json')

                with open(json_path) as json_file:
                    scores = json.load(json_file)

                criteria_scores[criteria].append(scores[score_name])
            criteria_scores['baseline'].append(baseline)

    fig, ax = plt.subplots()
    for key, values in criteria_scores.items():
        if len(values) > 0:
            if key == 'baseline':
                ax.plot(args.percentiles, values, label=key, linestyle='dashed')
            else:
                ax.plot(args.percentiles, values, label=key)
    legend = ax.legend()
    plt.title(f'{score_name}')
    fig_path = join(args.results_path, f'compare_{args.action}_filter_{score_name}.png')
    print(f'Saving plot to {fig_path}')
    plt.savefig(fig_path)

    pass


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="Filter unlikely fibers.")
    parser.add_argument("--action", type=str, default='bundle_filter',
                        choices=['bundle_filter', 'fiber_filter', 'track_vis'])
    parser.add_argument("results_path", type=str)
    parser.add_argument('--percentiles', nargs='+', type=int, default=[],
                        help="list of percentiles to try")
    parser.add_argument('--criteria', nargs='+', type=str, default=FILTERS,
                        help="list of criteria to try")
    parser.add_argument('--max_curv', nargs='+', type=str, default=[],
                        help="list of criteria to try")
    args = parser.parse_args()

    compare(args)
