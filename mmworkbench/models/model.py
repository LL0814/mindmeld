# -*- coding: utf-8 -*-
"""This module contains base classes for models defined in the models subpackage."""
from __future__ import absolute_import, unicode_literals
from builtins import object, super

from collections import namedtuple
import logging
import json
import math

import numpy as np
from sklearn.model_selection import (KFold, GroupShuffleSplit, GroupKFold, GridSearchCV,
                                     ShuffleSplit, StratifiedKFold, StratifiedShuffleSplit)

from sklearn.metrics import (f1_score, precision_recall_fscore_support as score, confusion_matrix,
                             accuracy_score)
from .helpers import (get_feature_extractor, get_label_encoder, register_label, ENTITIES_LABEL_TYPE,
                      entity_seqs_equal)
from .taggers.taggers import (get_tags_from_entities, get_entities_from_tags, get_boundary_counts,
                              BoundaryCounts)
logger = logging.getLogger(__name__)

# model scoring type
LIKELIHOOD_SCORING = 'log_loss'

_NEG_INF = -1e10


class ModelConfig(object):
    """A value object representing a model configuration.

    Attributes:
        model_type (str): The name of the model type. Will be used to find the
            model class to instantiate
        example_type (str): The type of the examples which will be passed into
            `fit()` and `predict()`. Used to select feature extractors
        label_type (str): The type of the labels which will be passed into
            `fit()` and returned by `predict()`. Used to select the label encoder
        model_settings (dict): Settings specific to the model type specified
        params (dict): Params to pass to the underlying classifier
        param_selection (dict): Configuration for param selection (using cross
            validation)
            {'type': 'shuffle',
            'n': 3,
            'k': 10,
            'n_jobs': 2,
            'scoring': '',
            'grid': {}
            }
        features (dict): The keys are the names of feature extractors and the
            values are either a kwargs dict which will be passed into the
            feature extractor function, or a callable which will be used as to
            extract features
    """

    __slots__ = ['model_type', 'example_type', 'label_type', 'features', 'model_settings', 'params',
                 'param_selection']

    def __init__(self, model_type=None, example_type=None, label_type=None, features=None,
                 model_settings=None, params=None, param_selection=None):
        for arg, val in {'model_type': model_type, 'example_type': example_type,
                         'label_type': label_type, 'features': features}.items():
            if val is None:
                raise TypeError('__init__() missing required argument {!r}'.format(arg))
        if params is None and (param_selection is None or param_selection.get('grid') is None):
            raise ValueError("__init__() One of 'params' and 'param_selection' is required")
        self.model_type = model_type
        self.example_type = example_type
        self.label_type = label_type
        self.features = features
        self.model_settings = model_settings
        self.params = params
        self.param_selection = param_selection

    def to_dict(self):
        """Converts the model config object into a dict

        Returns:
            dict: A dict version of the config
        """
        result = {}
        for attr in self.__slots__:
            result[attr] = getattr(self, attr)
        return result

    def __repr__(self):
        args_str = ', '.join("{}={!r}".format(key, getattr(self, key)) for key in self.__slots__)
        return "{}({})".format(self.__class__.__name__, args_str)

    def to_json(self):
        """Converts the model config object to JSON

        Returns:
            str: JSON representation of the classifier
        """
        return json.dumps(self.to_dict(), sort_keys=True)

    def required_resources(self):
        """Returns the resources this model requires

        Returns:
            set: set of required resources for this model
        """
        # get list of resources required by feature extractors
        required_resources = set()
        for name in self.features:
            feature = get_feature_extractor(self.example_type, name)
            required_resources.update(feature.__dict__.get('requirements', []))
        return required_resources


class EvaluatedExample(namedtuple('EvaluatedExample', ['example', 'expected', 'predicted',
                                                       'probas', 'label_type'])):
    """Represents the evaluation of a single example

    Attributes:
        example: The example being evaluated
        expected: The expected label for the example
        predicted: The predicted label for the example
        proba (dict): Maps labels to their predicted probabilities
        label_type (str): One of CLASS_LABEL_TYPE or ENTITIES_LABEL_TYPE
    """

    @property
    def is_correct(self):
        # For entities compare just the type, span and text for each entity.
        if self.label_type == ENTITIES_LABEL_TYPE:
            return entity_seqs_equal(self.expected, self.predicted)
        # For other label_types compare the full objects
        else:
            return self.expected == self.predicted


class RawResults():
    """Represents the raw results of a set of evaluated examples. Useful for generating
    stats and graphs.

    Attributes:
        predicted (list): A list of predictions. For sequences this is a list of lists, and for
                          standard classifieris this is a 1d array. All classes are in their numeric
                          representations for ease of use with evaluation libraries and graphing.
        expected (list): Same as predicted but contains the true or gold values.
        text_labels (list): A list of all the text label values, the index of the text label in
                             this array is the numeric label
        predicted_flat (list): (Optional): For sequence models this is a flattened list of all
                                predicted tags (1d array)
        expected_flat (list): (Optional): For sequence models this is a flattened list of all gold
                              tags
    """
    def __init__(self, predicted, expected, text_labels, predicted_flat=None, expected_flat=None):
        self.predicted = predicted
        self.expected = expected
        self.text_labels = text_labels
        self.predicted_flat = predicted_flat
        self.expected_flat = expected_flat


class ModelEvaluation(namedtuple('ModelEvaluation', ['config', 'results'])):
    """Represents the evaluation of a model at a specific configuration
    using a collection of examples and labels

    Attributes:
        config (ModelConfig): The model config used during evaluation
        results (list of EvaluatedExample): A list of the evaluated examples
    """
    def __init__(self, config, results):
        self.label_encoder = get_label_encoder(config)

    def get_accuracy(self):
        """The accuracy represents the share of examples whose predicted labels
        exactly matched their expected labels.

        Returns:
            float: The accuracy of the model
        """
        num_examples = len(self.results)
        num_correct = len([e for e in self.results if e.is_correct])
        return float(num_correct) / float(num_examples)

    def __repr__(self):
        num_examples = len(self.results)
        num_correct = len(list(self.correct_results()))
        accuracy = self.get_accuracy()
        msg = "<{} score: {:.2%}, {} of {} example{} correct>"
        return msg.format(self.__class__.__name__, accuracy, num_correct, num_examples,
                          '' if num_examples == 1 else 's')

    def correct_results(self):
        """
        Returns:
            iterable: Collection of the examples which were correct
        """
        for result in self.results:
            if result.is_correct:
                yield result

    def incorrect_results(self):
        """
        Returns:
            iterable: Collection of the examples which were incorrect
        """
        for result in self.results:
            if not result.is_correct:
                yield result

    def get_stats(self):
        """
        Returns a structured stats object for evaluation.

        Returns:
            dict: Structured dict containing evaluation statistics. Contains precision,
                  recall, f scores, support, etc.
        """
        raise NotImplementedError

    def print_stats(self):
        """
        Prints a useful stats table for evaluation.

        Returns:
            dict: Structured dict containing evaluation statistics. Contains precision,
                  recall, f scores, support, etc.
        """
        raise NotImplementedError

    def print_graphs(self):
        """
        Generates some graphs to help with evaluating of models.

        Returns:
            dict: Structured dict containing any arrays necessary to recreate the graphs.
        """
        raise NotImplementedError

    def raw_results(self):
        """
        Exposes raw vectors of expected and predicted for data scientists to use for any additional
        evaluation metrics or to generate graphs of their choice.

        Returns:
            NamedTuple: RawResults named tuple containing
                expected: vector of predicted classes (numeric value)
                predicted: vector of gold classes (numeric value)
                text_labels: a list of all the text label values, the index of the text label in
                this array is the numeric label
        """
        raise NotImplementedError

    def _update_raw_result(self, label, text_labels, vec):
        """
        Helper method for updating the text to numeric label vectors

        Returns:
            text_labels: The updated text_labels array
            vec: The updated label vector with the given label appended
        """
        if label not in text_labels:
            text_labels.append(label)
        vec.append(text_labels.index(label))
        return text_labels, vec

    def _get_common_stats(self, raw_expected, raw_predicted, text_labels):
        """
        Prints a useful stats table and returns a structured stats object for evaluation.

        Returns:
            dict: Structured dict containing evaluation statistics. Contains precision,
                  recall, f scores, support, etc.
        """
        labels = range(len(text_labels))

        confusion_stats = self._get_confusion_matrix_and_counts(y_true=raw_expected,
                                                                y_pred=raw_predicted)
        stats_overall = self._get_overall_stats(y_true=raw_expected,
                                                y_pred=raw_predicted,
                                                labels=labels)
        counts_overall = confusion_stats['counts_overall']
        stats_overall['tp'] = counts_overall.tp
        stats_overall['tn'] = counts_overall.tn
        stats_overall['fp'] = counts_overall.fp
        stats_overall['fn'] = counts_overall.fn

        class_stats = self._get_class_stats(y_true=raw_expected, y_pred=raw_predicted,
                                            labels=labels)
        counts_by_class = confusion_stats['counts_by_class']

        class_stats['tp'] = counts_by_class.tp
        class_stats['tn'] = counts_by_class.tn
        class_stats['fp'] = counts_by_class.fp
        class_stats['fn'] = counts_by_class.fn

        return {'stats_overall': stats_overall,
                'class_labels': text_labels,
                'class_stats': class_stats,
                'confusion_matrix': confusion_stats['confusion_matrix']}

    def _get_class_stats(self, y_true, y_pred, labels):
        """
        Method for getting some basic statistics by class.

        Returns:
            dict: A structured dictionary containing precision, recall, f_beta, and support
                  vectors (1 x number of classes)
        """
        precision, recall, f_beta, support = score(y_true=y_true, y_pred=y_pred,
                                                   labels=labels)

        stats = {
                    'precision': precision,
                    'recall': recall,
                    'f_beta': f_beta,
                    'support': support
                }
        return stats

    def _get_overall_stats(self, y_true, y_pred, labels):
        """
        Method for getting some overall statistics.

        Returns:
            dict: A structured dictionary containing scalar values for f1 scores and overall
                  accuracy.
        """
        f1_weighted = f1_score(y_true=y_true, y_pred=y_pred, labels=labels, average='weighted')
        f1_macro = f1_score(y_true=y_true, y_pred=y_pred, labels=labels, average='macro')
        f1_micro = f1_score(y_true=y_true, y_pred=y_pred, labels=labels, average='micro')
        accuracy = accuracy_score(y_true=y_true, y_pred=y_pred)

        stats_overall = {
            'f1_weighted': f1_weighted,
            'f1_macro': f1_macro,
            'f1_micro': f1_micro,
            'accuracy': accuracy
        }
        return stats_overall

    def _get_confusion_matrix_and_counts(self, y_true, y_pred):
        """
        Generates the confusion matrix where each element Cij is the number of observations known to
        be in group i predicted to be in group j

        Returns:
            dict: Contains 2d array of the confusion matrix, and an array of tp, tn, fp, fn values
        """
        confusion_mat = confusion_matrix(y_true=y_true, y_pred=y_pred)
        tp_arr, tn_arr, fp_arr, fn_arr = [], [], [], []

        num_classes = len(confusion_mat)
        for class_index in range(num_classes):
            # tp is C_classindex, classindex
            tp = confusion_mat[class_index][class_index]
            tp_arr.append(tp)

            # tn is the sum of Cij where i or j are not class_index
            mask = np.ones((num_classes, num_classes))
            mask[:, class_index] = 0
            mask[class_index, :] = 0
            tn = np.sum(mask*confusion_mat)
            tn_arr.append(tn)

            # fp is the sum of Cij where j is class_index but i is not
            mask = np.zeros((num_classes, num_classes))
            mask[:, class_index] = 1
            mask[class_index, class_index] = 0
            fp = np.sum(mask*confusion_mat)
            fp_arr.append(fp)

            # fn is the sum of Cij where i is class_index but j is not
            mask = np.zeros((num_classes, num_classes))
            mask[class_index, :] = 1
            mask[class_index, class_index] = 0
            fn = np.sum(mask*confusion_mat)
            fn_arr.append(fn)

        Counts = namedtuple('Counts', ['tp', 'tn', 'fp', 'fn'])
        return {'confusion_matrix': confusion_mat,
                'counts_by_class': Counts(tp_arr, tn_arr, fp_arr, fn_arr),
                'counts_overall': Counts(sum(tp_arr), sum(tn_arr), sum(fp_arr),
                                         sum(fn_arr))
                }

    def _print_class_stats_table(self, stats, text_labels, title='Statistics by class'):
        """
        Helper for printing a human readable table for class statistics

        Returns:
            None
        """
        title_format = "{:>20}" + "{:>12}" * (len(stats))
        common_stats = ['f_beta', 'precision', 'recall', 'support', 'tp', 'tn', 'fp', 'fn']
        stat_row_format = "{:>20}" + "{:>12.3f}" * 3 + "{:>12.0f}" * 5 + \
                          "{:>12.3f}" * (len(stats) - len(common_stats))
        table_titles = common_stats + [stat for stat in stats.keys()
                                       if stat not in common_stats]
        print(title + ": \n")
        print(title_format.format("class", *table_titles))
        for label in range(len(text_labels)):
            row = []
            for stat in table_titles:
                row.append(stats[stat][label])
            print(stat_row_format.format(self._truncate_label(text_labels[label], 18), *row))
        print("\n\n")

    def _print_class_matrix(self, matrix, text_labels):
        """
        Helper for printing a human readable class by class table for displaying
        a confusion matrix

        Returns:
            None
        """
        # Doesn't print if there isn't enough space to display the full matrix.
        if len(text_labels) > 10:
            print("Not printing confusion matrix since it is too large. The full matrix is still"
                  " included in the dictionary returned from get_stats().")
            return
        labels = range(len(text_labels))
        title_format = "{:>15}" * (len(labels)+1)
        stat_row_format = "{:>15}" * (len(labels)+1)
        table_titles = [self._truncate_label(text_labels[label], 10) for label in labels]
        print("Confusion matrix: \n")
        print(title_format.format("", *table_titles))
        for label in range(len(text_labels)):
            print(stat_row_format.format(self._truncate_label(text_labels[label], 10),
                                         *matrix[label]))
        print("\n\n")

    def _print_overall_stats_table(self, stats_overall, title='Overall statistics'):
        """
        Helper for printing a human readable table for overall statistics

        Returns:
            None
        """
        title_format = "{:>12}" * (len(stats_overall))
        common_stats = ['accuracy', 'f1_weighted', 'tp', 'tn', 'fp', 'fn']
        stat_row_format = "{:>12.3f}" * 2 + "{:>12.0f}" * 4 + \
                          "{:>12.3f}" * (len(stats_overall) - len(common_stats))
        table_titles = common_stats + [stat for stat in stats_overall.keys()
                                       if stat not in common_stats]
        print(title + ": \n")
        print(title_format.format(*table_titles))
        row = []
        for stat in table_titles:
            row.append(stats_overall[stat])
        print(stat_row_format.format(*row))
        print("\n\n")

    def _truncate_label(self, label, max_len):
        return (label[:max_len] + '..') if len(label) > max_len else label


class StandardModelEvaluation(ModelEvaluation):
    def raw_results(self):
        text_labels = []
        predicted, expected = [], []

        for result in self.results:
            text_labels, predicted = self._update_raw_result(result.predicted, text_labels,
                                                             predicted)
            text_labels, expected = self._update_raw_result(result.expected, text_labels, expected)

        return RawResults(predicted=predicted, expected=expected, text_labels=text_labels)

    def get_stats(self):
        raw_results = self.raw_results()
        stats = self._get_common_stats(raw_results.expected,
                                       raw_results.predicted,
                                       raw_results.text_labels)
        # Note can add any stats specific to the standard model to any of the tables here

        return stats

    def print_stats(self):
        raw_results = self.raw_results()
        stats = self.get_stats()

        self._print_overall_stats_table(stats['stats_overall'])
        self._print_class_stats_table(stats['class_stats'], raw_results.text_labels)
        self._print_class_matrix(stats['confusion_matrix'], raw_results.text_labels)

    def print_graphs(self):
        """
        TODO generate graphs from matplotlib/scikit learn
        """
        return None


class SequenceModelEvaluation(ModelEvaluation):
    def __init__(self, config, results):
        self._tag_scheme = config.model_settings.get('tag_scheme', 'IOB').upper()
        super().__init__(config, results)

    def raw_results(self):
        text_labels = []
        predicted, expected = [], []
        predicted_flat, expected_flat = [], []

        for result in self.results:
            raw_predicted = self.label_encoder.encode([result.predicted],
                                                      examples=[result.example])[0]
            raw_expected = self.label_encoder.encode([result.expected],
                                                     examples=[result.example])[0]

            vec = []
            for entity in raw_predicted:
                text_labels, vec = self._update_raw_result(entity, text_labels, vec)
            predicted.append(vec)
            predicted_flat.extend(vec)
            vec = []
            for entity in raw_expected:
                text_labels, vec = self._update_raw_result(entity, text_labels, vec)
            expected.append(vec)
            expected_flat.extend(vec)
        return RawResults(predicted=predicted, expected=expected,
                          text_labels=text_labels, predicted_flat=predicted_flat,
                          expected_flat=expected_flat)

    def _get_sequence_stats(self, y_true, y_pred, text_labels):
        """
        TODO: Generate additional sequence level stats
        """
        sequence_accuracy = self.get_accuracy()
        return {'sequence_accuracy': sequence_accuracy}

    def _print_sequence_stats_table(self, sequence_stats):
        """
        Helper for printing a human readable table for sequence statistics

        Returns:
            None
        """
        title_format = "{:>18}" * (len(sequence_stats))
        table_titles = ['sequence_accuracy']
        stat_row_format = "{:>18.3f}" * (len(sequence_stats))
        print("Sequence-level statistics: \n")
        print(title_format.format(*table_titles))
        row = []
        for stat in table_titles:
            row.append(sequence_stats[stat])
        print(stat_row_format.format(*row))
        print("\n\n")

    def get_stats(self):
        raw_results = self.raw_results()
        stats = self._get_common_stats(raw_results.expected_flat,
                                       raw_results.predicted_flat,
                                       raw_results.text_labels)
        sequence_stats = self._get_sequence_stats(y_true=raw_results.expected,
                                                  y_pred=raw_results.predicted,
                                                  text_labels=raw_results.text_labels)
        stats['sequence_stats'] = sequence_stats

        # Note: can add any stats specific to the sequence model to any of the tables here
        return stats

    def print_stats(self):
        raw_results = self.raw_results()
        stats = self.get_stats()

        self._print_overall_stats_table(stats['stats_overall'], 'Overall tag-level statistics')
        self._print_class_stats_table(stats['class_stats'], raw_results.text_labels,
                                      'Tag-level statistics by class')
        self._print_class_matrix(stats['confusion_matrix'], raw_results.text_labels)
        self._print_sequence_stats_table(stats['sequence_stats'])

    def print_graphs(self):
        """
        TODO generate graphs from matplotlib/scikitlearn
        """
        return None


class EntityModelEvaluation(SequenceModelEvaluation):
    """Generates some statistics specific to entity recognition
    """
    def _get_entity_boundary_stats(self):
        """
        Calculate le, be, lbe, tp, tn, fp, fn as defined here:
        https://nlpers.blogspot.com/2006/08/doing-named-entity-recognition-dont.html
        """
        boundary_counts = BoundaryCounts()
        raw_results = self.raw_results()
        for expected_sequence, predicted_sequence in zip(raw_results.expected,
                                                         raw_results.predicted):
            expected_seq_labels = [raw_results.text_labels[i] for i in expected_sequence]
            predicted_seq_labels = [raw_results.text_labels[i] for i in predicted_sequence]
            boundary_counts = get_boundary_counts(expected_seq_labels, predicted_seq_labels,
                                                  boundary_counts)
        return boundary_counts.to_dict()

    def _print_boundary_stats(self, boundary_counts):
        title_format = "{:>12}" * (len(boundary_counts))
        table_titles = boundary_counts.keys()
        stat_row_format = "{:>12}" * (len(boundary_counts))
        print("Segment-level statistics: \n")
        print(title_format.format(*table_titles))
        row = []
        for stat in table_titles:
            row.append(boundary_counts[stat])
        print(stat_row_format.format(*row))
        print("\n\n")

    def get_stats(self):
        stats = super().get_stats()
        if self._tag_scheme == 'IOB':
            boundary_stats = self._get_entity_boundary_stats()
            stats['boundary_stats'] = boundary_stats
        return stats

    def print_stats(self):
        raw_results = self.raw_results()
        stats = self.get_stats()

        self._print_overall_stats_table(stats['stats_overall'], 'Overall tag-level statistics')
        self._print_class_stats_table(stats['class_stats'], raw_results.text_labels,
                                      'Tag-level statistics by class')
        self._print_class_matrix(stats['confusion_matrix'], raw_results.text_labels)
        if self._tag_scheme == 'IOB':
            self._print_boundary_stats(stats['boundary_stats'])
        self._print_sequence_stats_table(stats['sequence_stats'])


class Model(object):
    """An abstract class upon which all models are based.

    Attributes:
        config (ModelConfig): The configuration for the model
    """
    def __init__(self, config):
        self.config = config
        self._label_encoder = get_label_encoder(self.config)
        self._current_params = None
        self._resources = {}
        self._clf = None
        self.cv_loss_ = None

    def fit(self, examples, labels, params=None):
        raise NotImplementedError

    def _fit_cv(self, examples, labels, groups=None, selection_settings=None):
        """Called by the fit method when cross validation parameters are passed in. Runs cross
        validation and returns the best estimator and parameters.

        Args:
            examples (list): A list of examples. Should be in the format expected by the underlying
                             estimator.
            labels (list): The target output values.
            groups (None, optional): Same length as examples and labels. Used to group examples when
                                     splitting the dataset into train/test
            selection_settings (dict, optional): A dictionary containing the cross validation
                                                 selection settings.

        """
        selection_settings = selection_settings or self.config.param_selection
        cv_iterator = self._get_cv_iterator(selection_settings)

        if selection_settings is None:
            return self._fit(examples, labels, self.config.params), self.config.params

        cv_type = selection_settings['type']
        num_splits = cv_iterator.get_n_splits(examples, labels, groups)
        logger.info('Selecting hyperparameters using %s cross-validation with %s split%s', cv_type,
                    num_splits, '' if num_splits == 1 else 's')

        scoring = self._get_cv_scorer(selection_settings)
        n_jobs = selection_settings.get('n_jobs', -1)

        param_grid = self._convert_params(selection_settings['grid'], labels)
        model_class = self._get_model_constructor()
        estimator, param_grid = self._get_cv_estimator_and_params(model_class, param_grid)
        grid_cv = GridSearchCV(estimator=estimator, scoring=scoring, param_grid=param_grid,
                               cv=cv_iterator, n_jobs=n_jobs)
        model = grid_cv.fit(examples, labels, groups)

        for idx, params in enumerate(model.cv_results_['params']):
            logger.debug('Candidate parameters: {}'.format(params))
            std_err = 2.0 * model.cv_results_['std_test_score'][idx] / math.sqrt(model.n_splits_)
            if scoring == LIKELIHOOD_SCORING:
                msg = 'Candidate average log likelihood: {:.4} ± {:.4}'
            else:
                msg = 'Candidate average accuracy: {:.2%} ± {:.2%}'
            logger.info(msg.format(model.cv_results_['mean_test_score'][idx], std_err))

        if scoring == LIKELIHOOD_SCORING:
            msg = 'Best log likelihood: {:.4}, params: {}'
            self.cv_loss_ = - model.best_score_
        else:
            msg = 'Best accuracy: {:.2%}, params: {}'
            self.cv_loss_ = 1 - model.best_score_

        best_params = self._process_cv_best_params(model.best_params_)
        logger.info(msg.format(model.best_score_, best_params))

        return model.best_estimator_, model.best_params_

    def _get_cv_scorer(self, selection_settings):
        """
        Returns the scorer to use based on the selection settings and classifier type.
        """
        raise NotImplementedError

    def _get_cv_estimator_and_params(self, model_class, param_grid):
        return model_class(), param_grid

    def _process_cv_best_params(self, best_params):
        return best_params

    def select_params(self, examples, labels, selection_settings=None):
        raise NotImplementedError

    def _convert_params(self, param_grid, y, is_grid=True):
        """Convert the params from the style given by the config to the style
        passed in to the actual classifier.

        Args:
            param_grid (dict): lists of classifier parameter values, keyed by
                parameter name
            y (list): A list of labels
            is_grid (bool, optional): Indicates whether param_grid is actually a grid
                or a params dict.
        """
        raise NotImplementedError

    def predict(self, examples):
        raise NotImplementedError

    def predict_proba(self, examples):
        raise NotImplementedError

    def predict_log_proba(self, examples):
        raise NotImplementedError

    def evaluate(self, examples, labels):
        raise NotImplementedError

    def _get_effective_config(self):
        """Create a model config object for the current effective config (after
        param selection)

        Returns:
            ModelConfig
        """
        config_dict = self.config.to_dict()
        config_dict.pop('param_selection')
        config_dict['params'] = self._current_params
        return ModelConfig(**config_dict)

    def register_resources(self, **kwargs):
        """Registers resources which are accessible to feature extractors

        Args:
            **kwargs: dictionary of resources to register

        """
        self._resources.update(kwargs)

    def get_feature_matrix(self, examples, y=None, fit=False):
        raise NotImplementedError

    def _extract_features(self, example):
        """Gets all features from an example.

        Args:
            example: An example object.

        Returns:
            (dict of str: number): A dict of feature names to their values.
        """
        example_type = self.config.example_type
        feat_set = {}
        for name, kwargs in self.config.features.items():
            if callable(kwargs):
                # a feature extractor function was passed in directly
                feat_extractor = kwargs
            else:
                feat_extractor = get_feature_extractor(example_type, name)(**kwargs)
            feat_set.update(feat_extractor(example, self._resources))
        return feat_set

    def _get_cv_iterator(self, settings):
        if not settings:
            return None
        cv_type = settings['type']
        try:
            cv_iterator = {"k-fold": self._k_fold_iterator,
                           "shuffle": self._shuffle_iterator,
                           "group-k-fold": self._groups_k_fold_iterator,
                           "group-shuffle": self._groups_shuffle_iterator,
                           "stratified-k-fold": self._stratified_k_fold_iterator,
                           "stratified-shuffle": self._stratified_shuffle_iterator,
                           }.get(cv_type)(settings)
        except KeyError:
            raise ValueError('Unknown param selection type: {!r}'.format(cv_type))

        return cv_iterator

    @staticmethod
    def _k_fold_iterator(settings):
        k = settings['k']
        return KFold(n_splits=k)

    @staticmethod
    def _shuffle_iterator(settings):
        k = settings['k']
        n = settings.get('n', k)
        test_size = 1.0 / k
        return ShuffleSplit(n_splits=n, test_size=test_size)

    @staticmethod
    def _groups_k_fold_iterator(settings):
        k = settings['k']
        return GroupKFold(n_splits=k)

    @staticmethod
    def _groups_shuffle_iterator(settings):
        k = settings['k']
        n = settings.get('n', k)
        test_size = 1.0 / k
        return GroupShuffleSplit(n_splits=n, test_size=test_size)

    @staticmethod
    def _stratified_k_fold_iterator(settings):
        k = settings['k']
        return StratifiedKFold(n_splits=k)

    @staticmethod
    def _stratified_shuffle_iterator(settings):
        k = settings['k']
        n = settings.get('n', k)
        test_size = 1.0 / k
        return StratifiedShuffleSplit(n_splits=n, test_size=test_size)

    def requires_resource(self, resource):
        example_type = self.config.example_type
        for name, kwargs in self.config.features.items():
            if callable(kwargs):
                # a feature extractor function was passed in directly
                feature_extractor = kwargs
            else:
                feature_extractor = get_feature_extractor(example_type, name)
            if ('requirements' in feature_extractor.__dict__ and
                    resource in feature_extractor.requirements):
                return True
        return False

    def initialize_resources(self, resource_loader, examples=None, labels=None):
        """Load the required resources for feature extractors. Each feature extractor uses
        @requires decorator to declare required resources. Based on feature list in model config
        a list of required resources are compiled, and the passed in resource loader is then used to
        load the resources accordingly.
        Args:
            resource_loader (ResourceLoader): application resource loader object
            examples (list): Optional. A list of examples.
            labels (list): Optional. A parallel list to examples. The gold labels
                           for each example.
        """

        # get list of resources required by feature extractors
        required_resources = self.config.required_resources()

        # load required resources if not present in model resources
        for rname in required_resources:
            if rname not in self._resources:
                self._resources[rname] = resource_loader.load_feature_resource(
                    rname, queries=examples, labels=labels)


class LabelEncoder(object):
    """The label encoder is responsible for converting between rich label
    objects such as a ProcessedQuery and basic formats a model can interpret.

    A workbench model use its label encoder at fit time to encode labels into a
    form it can deal with, and at predict time to decode predictions into
    objects
    """
    def __init__(self, config):
        """Initializes an encoder

        Args:
            config (ModelConfig): The model
        """
        self.config = config

    def encode(self, labels, **kwargs):
        """Transforms a list of label objects into a vector of classes.


        Args:
            labels (list): A list of labels to encode
        """
        return labels

    def decode(self, classes, **kwargs):
        """Decodes a vector of classes into a list of labels

        Args:
            classes (list): A list of classes

        Returns:
            list: The decoded labels
        """
        return classes


class EntityLabelEncoder(LabelEncoder):

    def _get_tag_scheme(self):
        return self.config.model_settings.get('tag_scheme', 'IOB').upper()

    def encode(self, labels, **kwargs):
        examples = kwargs['examples']
        scheme = self._get_tag_scheme()
        # Here each label is a list of entities for the corresponding example
        all_tags = []
        for idx, label in enumerate(labels):
            all_tags.append(get_tags_from_entities(examples[idx], label, scheme))
        return all_tags

    def decode(self, tags_by_example, **kwargs):
        # TODO: support decoding multiple queries at once
        scheme = self._get_tag_scheme()
        examples = kwargs['examples']
        labels = [get_entities_from_tags(examples[idx], tags, scheme)
                  for idx, tags in enumerate(tags_by_example)]
        return labels


register_label('class', LabelEncoder)
register_label('entities', EntityLabelEncoder)
