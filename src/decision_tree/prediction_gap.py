from multiprocessing import Pool
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import norm

import src.decision_tree.tree as tree
from src.decision_tree.tree import TreeEnsemble


class PerturbPredictionGap:
    def __init__(self, stddev):
        self.stddev = stddev

    def _compute_cdf(self, data_point, perturbed_features: set):
        cdf_dict = dict()
        for feature, value in data_point.items():
            if feature in perturbed_features:
                cdf_dict[feature] = lambda x, v=value: norm.cdf(
                    x, loc=v, scale=self.stddev
                )
            else:
                cdf_dict[feature] = lambda x, t=np.float32(value): 0.0 if x < t else 1.0
        return cdf_dict

    def prediction_gap_fixed(
        self,
        model: tree.Model,
        data_point,
        perturbed_features: set,
        baseline_pred,
        index: int = 0,
    ):
        result = model.expected_diff_squared(
            self._compute_cdf(data_point, perturbed_features), baseline_pred
        )
        # print(f"Datapoint {index} returned predgap value of {result}.")
        return result

    def pgi(self, model: tree.Model, data_point, baseline_pred, sorted_features: list):
        n = len(sorted_features)
        result = 0
        for k in range(1, n + 1):
            result += self.prediction_gap_fixed(
                model, data_point, set(sorted_features[0:k]), baseline_pred
            )
        result /= n
        return result

    def pgu(self, model: tree.Model, data_point, baseline_pred, sorted_features: list):
        return self.pgi(model, data_point, baseline_pred, sorted_features[::-1])

    def prediction_gap_single_f(
        self, model: tree.Model, data_point, perturbed_feature, f
    ):
        return model.expected_single_feature(
            data_point,
            perturbed_feature,
            self._compute_cdf(data_point, {perturbed_feature})[perturbed_feature],
            f,
        )

    def prediction_gap_single_abs(
        self, model: tree.Model, data_point, perturbed_feature, baseline
    ):
        return self.prediction_gap_single_f(
            model, data_point, perturbed_feature, lambda x: abs(x - baseline)
        )

    def prediction_gap_single_squared(
        self, model: tree.Model, data_point, perturbed_feature, baseline
    ):
        return self.prediction_gap_single_f(
            model, data_point, perturbed_feature, lambda x: (x - baseline) ** 2
        )

    def rank_features(self, model: tree.Model, data_point: pd.Series) -> list:
        """Returns a list of features in order from most to least important.

        data_point should be a row from a dataframe, without the last (predicted) variable,
        e.g. data_point = data.iloc[i, :-1] should be good.
        """
        curr_features = set(data_point.index)
        ranked_features = []
        baseline = model.eval(data_point)
        while len(curr_features) > 1:
            predgap_dict = {}
            for feature in curr_features:
                tmp = self.prediction_gap_fixed(
                    model, data_point, set(ranked_features) | {feature}, baseline
                )
                predgap_dict[feature] = tmp
                # print(feature, tmp)
            best_feature = max(
                predgap_dict, key=predgap_dict.get
            )  # this is the current feature with max predgap
            ranked_features.append(best_feature)
            # print(f"Rank {len(ranked_features)}: {best_feature}.")
            curr_features -= {best_feature}
        ranked_features.append(
            list(curr_features)[0]
        )  # this appends the last (and thus lowest ranked) feature
        print(ranked_features)
        return ranked_features


class NormalPredictionGap(PerturbPredictionGap):
    def __init__(self, stddev):
        self.stddev = stddev

    def _compute_cdf(self, data_point, perturbed_features: set):
        cdf_dict = dict()
        for feature, value in data_point.items():
            if feature in perturbed_features:
                cdf_dict[feature] = lambda x, v=value: norm.cdf(
                    x, loc=v, scale=self.stddev
                )
            else:
                cdf_dict[feature] = lambda x, t=np.float32(value): 0.0 if x < t else 1.0
        return cdf_dict


def prediction_gap_on_single_feature_perturbation(
    predgap: PerturbPredictionGap,
    trees: TreeEnsemble,
    data: pd.DataFrame,
    features: Optional[set] = None,
    squared: bool = False,
) -> pd.DataFrame:
    """Calculate prediction gap of each feature using the provided dataset."""
    if features is None:
        features = set(data.columns[:-1])
    func = (
        predgap.prediction_gap_single_squared
        if squared
        else predgap.prediction_gap_single_abs
    )
    results = []
    baseline_preds = trees.eval_on_multiple_rows(data)
    for feature in features:
        print(f"Starting predgap calculation for {feature}.")
        curr_feature_total = 0.0
        for i in range(len(data)):
            x = data.iloc[i, :-1]
            y = baseline_preds[i]
            curr_feature_total += func(trees, x, feature, y)
        curr_feature_total /= len(data)
        results.append((feature, curr_feature_total))
    return pd.DataFrame(results, columns=["Feature", "PredGap"]).sort_values(
        "PredGap", ascending=False
    )


def prediction_gap_by_random_sampling(
    trees: TreeEnsemble,
    data: pd.DataFrame,
    perturbed_features: set,
    stddev: float = 1.0,
    squared: bool = False,
    seed: Optional[int] = None,
    num_iter: int = 100,
) -> np.array:
    def normal_perturbation(df: pd.DataFrame):
        perturbed_df = df.copy()
        perturbed_df[list(perturbed_features)] += rng.normal(
            loc=0.0, scale=stddev, size=(len(df), len(perturbed_features))
        )
        return perturbed_df

    y = trees.eval_on_multiple_rows(data)
    rng = np.random.default_rng(seed=seed)
    print(f"There are {len(data)} datapoints in the dataset.")
    print(
        f"The following {len(perturbed_features)} features are subject to perturbation:"
    )
    print(perturbed_features)
    print(
        f"Starting prediction gap calculation by random sampling with {num_iter} iterations."
    )
    results = np.zeros(len(data))
    for i in range(num_iter):
        perturbed_y = trees.eval_on_multiple_rows(normal_perturbation(data))
        results += (perturbed_y - y) ** 2 if squared else np.abs(perturbed_y - y)
    results /= num_iter
    # return np.mean(results)
    return results


def prediction_gap_by_random_sampling_single_datapoint(
    trees: TreeEnsemble,
    data_point: pd.Series,
    perturbed_features: set,
    stddev: float = 1.0,
    squared: bool = False,
    seed: Optional[int] = None,
    num_iter: int = 100,
) -> float:
    def normal_perturbation(dp: pd.Series):
        perturbed_dp = dp.copy()
        perturbed_dp[list(perturbed_features)] += rng.normal(
            loc=0.0, scale=stddev, size=len(perturbed_features)
        )
        return perturbed_dp

    y = trees.eval(data_point)
    rng = np.random.default_rng(seed=seed)
    result = 0.0
    for i in range(num_iter):
        perturbed_y = trees.eval(normal_perturbation(data_point))
        result += (perturbed_y - y) ** 2 if squared else np.abs(perturbed_y - y)
    result /= num_iter
    return result


def rank_features_by_random(
    trees: TreeEnsemble,
    data_point: pd.Series,
    stddev: float = 1.0,
    num_iter: int = 100,
    seed: Optional[int] = None,
) -> list:
    curr_features = set(data_point.index)
    ranked_features = []
    while len(curr_features) > 1:
        predgap_dict = {}
        for feature in curr_features:
            tmp = prediction_gap_by_random_sampling_single_datapoint(
                trees,
                data_point,
                set(ranked_features) | {feature},
                stddev=stddev,
                squared=True,
                seed=seed,
                num_iter=num_iter,
            )
            predgap_dict[feature] = tmp
            # print(feature, tmp)
        best_feature = max(
            predgap_dict, key=predgap_dict.get
        )  # this is the current feature with max predgap
        ranked_features.append(best_feature)
        # print(f"Rank {len(ranked_features)}: {best_feature}.")
        curr_features -= {best_feature}
    ranked_features.append(
        list(curr_features)[0]
    )  # this appends the last (and thus lowest ranked) feature
    print(ranked_features)
    return ranked_features


def prediction_gap_by_exact_calc(
    predgap: PerturbPredictionGap,
    trees: TreeEnsemble,
    data: pd.DataFrame,
    perturbed_features: set,
    squared: bool = False,
    processes: int = 4,
):
    if not squared:
        raise NotImplementedError("")
    baseline_preds = trees.eval_on_multiple_rows(data)
    print(f"There are {len(data)} datapoints in the dataset.")
    print(
        f"The following {len(perturbed_features)} features are subject to perturbation:"
    )
    print(perturbed_features)
    print("Starting exact prediction gap calculation.")
    results = []
    args = []
    for i in range(len(data)):
        x = data.iloc[i, :-1]
        y = baseline_preds[i]
        args.append((trees, x, perturbed_features, y, i))
    pool = Pool(processes=processes)
    results = np.array(pool.starmap(predgap.prediction_gap_fixed, args))
    # for i in range(len(data)):
    #     x = data.iloc[i, :-1]
    #     y = baseline_preds[i]
    #     results.append(predgap.prediction_gap_fixed(trees, x, perturbed_features, y))
    #     print(f"Datapoint {i} returned predgap value of {results[-1]}.")
    return results


def prediction_gap_by_exact_calc_single_datapoint(
    predgap: PerturbPredictionGap,
    trees: TreeEnsemble,
    data: pd.DataFrame,
    perturbed_features: set,
    squared: bool = False,
):
    if not squared:
        raise NotImplementedError("")
    baseline_preds = trees.eval_on_multiple_rows(data)
    print(f"There are {len(data)} datapoints in the dataset.")
    print(
        f"The following {len(perturbed_features)} features are subject to perturbation:"
    )
    print(perturbed_features)
    print("Starting exact prediction gap calculation.")
    results = []
    for i in range(len(data)):
        x = data.iloc[i, :-1]
        y = baseline_preds[i]
        r = predgap.prediction_gap_fixed(trees, x, perturbed_features, y, i)
        results.append(r)
    # for i in range(len(data)):
   #     x = data.iloc[i, :-1]
    #     y = baseline_preds[i]
    #     results.append(predgap.prediction_gap_fixed(trees, x, perturbed_features, y))
    #     print(f"Datapoint {i} returned predgap value of {results[-1]}.")
    return np.array(results)
