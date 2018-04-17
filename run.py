import argparse
import gc
import itertools
import json
import time
import os
import time
from contextlib import contextmanager
from functools import partial
from multiprocessing.pool import Pool
from typing import List, Tuple

import pandas as pd
import pandas.testing

import features.time_series_click
from features import Feature
from features.basic import Ip, App, Os, Device, Channel, ClickHour, BasicCount, IsAttributed
from models import LightGBM, Model
from utils import dump_json_log

feature_map = {
    'ip': Ip,
    'app': App,
    'os': Os,
    'device': Device,
    'channel': Channel,
    'hour': ClickHour,
    'count': BasicCount,
    'is_attributed': IsAttributed,
    'future_click_count_1': features.time_series_click.generate_future_click_count(60),
    'future_click_count_10': features.time_series_click.generate_future_click_count(600),
    'past_click_count_10': features.time_series_click.generate_past_click_count(600),
    'future_click_count_80': features.time_series_click.generate_future_click_count(4800),
    'past_click_count_80': features.time_series_click.generate_past_click_count(4800),
    'future_click_ratio_10': features.time_series_click.generate_future_click_ratio(600),
    'past_click_ratio_10': features.time_series_click.generate_future_click_ratio(600),
    'future_click_ratio_80': features.time_series_click.generate_future_click_ratio(4800),
    'past_click_ratio_80': features.time_series_click.generate_future_click_ratio(4800),
    'next_click_time_delta': features.time_series_click.NextClickTimeDelta,
    'prev_click_time_delta': features.time_series_click.PrevClickTimeDelta,
    # 'next_click_time_delta_v2': features.time_series_click.NextClickTimeDeltaV2,
    # 'prev_click_time_delta_v2': features.time_series_click.PrevClickTimeDeltaV2,
    # 'next_click_time_delta_v3': features.time_series_click.NextClickTimeDeltaV3,
    # 'prev_click_time_delta_v3': features.time_series_click.PrevClickTimeDeltaV3,
    'exact_same_click': features.time_series_click.ExactSameClick,  # It will be duplicated with all id counts
    'exact_same_click_id': features.time_series_click.ExactSameClickId,
    'all_click_count': features.time_series_click.AllClickCount,
    'average_attributed_ratio': features.time_series_click.AverageAttributedRatio,
    'cumulative_click_count': features.time_series_click.CumulativeClickCount,
    'cumulative_click_count_future': features.time_series_click.CumulativeClickCountFuture,
    # 'next_channel': features.time_series_click.NextChannel,
    # 'prev_channel': features.time_series_click.PrevChannel,
    # 'next_app': features.time_series_click.NextApp,
    # 'prev_app': features.time_series_click.PrevApp
}

models = {
    'lightgbm': LightGBM
}

output_directory = 'data/output'

target_variable = 'is_attributed'


@contextmanager
def simple_timer(message):
    start_time = time.time()
    yield
    elapsed_time = time.time() - start_time
    print("{}: {:.3f} [s]".format(message, elapsed_time))


# Now we don't set index when loading training features because they should have been already down-sampled.
def load_dataset(paths, index=None) -> pd.DataFrame:
    assert len(paths) > 0

    feature_datasets = []
    for path in paths:
        if index is None:
            feature_datasets.append(pd.read_feather(path))
        else:
            feature_datasets.append(pd.read_feather(path).loc[index])
        gc.collect()
    # check if all of feature dataset share the same index
    index = feature_datasets[0].index
    for feature_dataset in feature_datasets[1:]:
        pandas.testing.assert_index_equal(index, feature_dataset.index)

    return pd.concat(feature_datasets, axis=1)


def get_dataset_filename(config, dataset_type: str) -> str:
    return os.path.join(config['dataset']['input_directory'], config['dataset']['files'][dataset_type])


def negative_down_sampling(data: pd.DataFrame, random_state: int):
    with simple_timer("Get positive data"):
        positive_data = data[data[target_variable] == 1]
        positive_ratio = float(len(positive_data)) / len(data)
    with simple_timer("Get negative data"):
        negative_data = data[data[target_variable] == 0].sample(
            frac=positive_ratio / (1 - positive_ratio), random_state=random_state)
    return positive_data.index.union(negative_data.index).sort_values()


def get_feature_list(config) -> List[str]:
    for feature in config['features']:
        assert feature in feature_map, "Unknown feature {}".format(feature)
    features = config['features']
    features.append(target_variable)
    return features


def get_feature(feature_name: str, config) -> Feature:
    cache_dir: str = config['dataset']['cache_directory']
    return feature_map[feature_name](cache_dir)


def get_random_state_indices(train_path: str, random_states: List[int]) -> List[Tuple[int, pd.Index]]:
    rs = []
    with simple_timer("Load training dataset in random sampled index calculation"):
        train_data = pd.read_feather(train_path)
    for random_state in random_states:
        train_index = negative_down_sampling(train_data, random_state=random_state)
        rs.append((random_state, train_index))
    return rs


def load_feature(feature_name: str, train_path: str, test_path: str, random_states: List[int], config) -> Tuple[List[str], str]:
    feature = get_feature(feature_name=feature_name, config=config)
    with simple_timer("Load random state indices"):
        random_states_ = get_random_state_indices(train_path, random_states)
    return feature.create_features(train_path, test_path, random_states=random_states_)


def load_features(config, random_states: List[int]) -> Tuple[List[List[str]], List[str]]:
    tr_path = get_dataset_filename(config, 'train')
    te_path = get_dataset_filename(config, 'test')
    train_feature_paths_lists = [[] for _ in random_states]
    test_feature_paths = []

    # We fix the number of processes to four because of memory limitation
    with Pool(4) as p:
        res = p.map(partial(load_feature, train_path=tr_path, test_path=te_path,
                            random_states=random_states, config=config), get_feature_list(config))
        for tr_feature_path_list, te_feature_path in res:
            for i, tr_feature_path in enumerate(tr_feature_path_list):
                train_feature_paths_lists[i].append(tr_feature_path)
            test_feature_paths.append(te_feature_path)
    return train_feature_paths_lists, test_feature_paths


def load_categorical_features(config) -> List[str]:
    return list(itertools.chain(*[feature_map[feature].categorical_features() for feature in config['features']]))


def load_test_dataset(test_path: str, test_feature_paths: List[str], id_mapper: pd.DataFrame) -> pd.DataFrame:
    assert len(test_feature_paths) > 0
    required_ids = set(id_mapper['old_click_id'])
    df_test = pd.read_feather(test_path)
    df_test = df_test[df_test.click_id.isin(required_ids)]
    test = load_dataset(test_feature_paths, df_test.index)
    test['click_id'] = df_test.click_id
    return test


def load_train_dataset(train_feature_paths: List[str]) -> pd.DataFrame:
    assert len(train_feature_paths) > 0
    print(train_feature_paths)
    df_train = load_dataset(train_feature_paths)
    assert 'is_attributed' in df_train.columns
    return df_train


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='./configs/lightgbm_0.json')
    options = parser.parse_args()
    config = json.load(open(options.config))
    assert config['model']['name'] in models  # check model's existence before getting datasets

    with simple_timer("Load click id mapping"):
        id_mapper_file = 'data/working/id_mapping.feather'
        assert os.path.exists(id_mapper_file), "Please download {} from s3 before running this script".format(
            id_mapper_file)
        id_mapper = pd.read_feather(id_mapper_file)

    model: Model = models[config['model']['name']]()
    predictions = []
    train_results = []
    categorical_features = load_categorical_features(config)
    negative_down_sampling_config = config['dataset']['negative_down_sampling']

    if not negative_down_sampling_config['enabled']:
        raise NotImplementedError("We should always downsample")

    with simple_timer("Create features"):
        sampled_train_feature_paths_list, test_feathre_paths = \
            load_features(config, list(range(negative_down_sampling_config['bagging_size'])))

    with simple_timer("Load test features"):
        test_data = load_test_dataset(get_dataset_filename(config, 'test'), test_feathre_paths, id_mapper)

    for i, sampled_train_feature_paths in enumerate(sampled_train_feature_paths_list):
        start_time = time.time()
        with simple_timer("Load train features"):
            sampled_train_dataset = load_train_dataset(sampled_train_feature_paths)

        valid_ratio = 0.9
        train_length = int(len(sampled_train_dataset) * valid_ratio)
        sampled_train_data = sampled_train_dataset[:train_length]
        sampled_valid_data = sampled_train_dataset[train_length:]
        predictors = sampled_train_data.columns.drop(target_variable)

        with simple_timer("Train model"):
            booster, result = model.train_and_predict(train=sampled_train_data,
                                                      valid=sampled_valid_data,
                                                      categorical_features=categorical_features,
                                                      target=target_variable,
                                                      params=config['model'])
        with simple_timer("Create prediction"):
            test_prediction_start_time = time.time()
            prediction = booster.predict(test_data[predictors])
            test_prediction_elapsed_time = time.time() - test_prediction_start_time

            valid_prediction_start_time = time.time()
            valid_prediction_elapsed_time = time.time() - valid_prediction_start_time
            predictions.append(prediction)

        # This only works when we are using LightGBM
        train_results.append({
            'train_auc': result['train']['auc'][booster.best_iteration],
            'valid_auc': result['valid']['auc'][booster.best_iteration],
            'best_iteration': booster.best_iteration,
            'train_time': time.time() - start_time,
            'prediction_time': {
                'test': test_prediction_elapsed_time,
                'valid': valid_prediction_elapsed_time
            },
            'feature_importance': {name: int(score) for name, score in
                                   zip(booster.feature_name(), booster.feature_importance())}
        })
        print("Finished processing {}-th bag: {}".format(i, str(train_results[-1])))

    test_data['prediction'] = sum(predictions) / len(predictions)
    old_click_to_prediction = {}
    for (click_id, prediction) in zip(test_data.click_id, test_data.prediction):
        old_click_to_prediction[click_id] = prediction

    click_ids = []
    predictions = []
    for (new_click_id, old_click_id) in zip(id_mapper.new_click_id, id_mapper.old_click_id):
        if old_click_id not in old_click_to_prediction:
            continue
        click_ids.append(new_click_id)
        predictions.append(old_click_to_prediction[old_click_id])
    submission = pd.DataFrame({'click_id': click_ids, '{}'.format(target_variable): predictions})
    submission_path = os.path.join(os.path.dirname(__file__), output_directory,
                                   os.path.basename(options.config) + '.submission.csv')
    submission.sort_values(by='click_id').to_csv(submission_path, index=False)
    dump_json_log(options, train_results, output_directory)


if __name__ == "__main__":
    main()
