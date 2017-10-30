"""
Example script demonstrating the training portion of the MLR pipeline.
This is mostly to demonstrate how everything ties together

To run:
    PYSPARK_PYTHON=venv/bin/python spark-submit \
        --jars /path/to/mjolnir-with-dependencies.jar
        --artifacts 'mjolnir_venv.zip#venv' \
        path/to/training_pipeline.py
"""

from __future__ import absolute_import
import argparse
import collections
import datetime
import glob
import logging
import mjolnir.feature_engineering
import mjolnir.training.xgboost
import os
import pickle
import sys
from pyspark import SparkContext
from pyspark.sql import HiveContext
from pyspark.sql import functions as F


def summarize_training_df(df, data_size):
    """Generate a summary of min/max/stddev/mean of data frame

    Parameters
    ----------
    df : pyspark.sql.DataFrame
        DataFrame used for training
    data_size : int
        Number of rows in df

    Returns
    -------
    dict
        Map from field name to a map of statistics about the field
    """
    if data_size > 10000000:
        df = df.repartition(200)
    summary = collections.defaultdict(dict)
    for row in mjolnir.feature_engineering.explode_features(df).describe().collect():
        statistic = row.summary
        for field, value in (x for x in row.asDict().items() if x[0] != 'summary'):
            summary[field][statistic] = value
    return dict(summary)


def run_pipeline(sc, sqlContext, input_dir, output_dir, wikis, initial_num_trees, final_num_trees,
                 num_workers, num_cv_jobs, num_folds, test_dir, zero_features):
    for wiki in wikis:
        print 'Training wiki: %s' % (wiki)
        df_hits_with_features = (
            sqlContext.read.parquet(input_dir)
            .where(F.col('wikiid') == wiki))

        data_size = df_hits_with_features.count()
        if data_size == 0:
            print 'No data found.' % (wiki)
            print ''
            continue

        if zero_features:
            df_hits_with_features = mjolnir.feature_engineering.zero_features(
                    df_hits_with_features, zero_features)

        tune_results = mjolnir.training.xgboost.tune(
            df_hits_with_features, num_folds=num_folds,
            num_cv_jobs=num_cv_jobs, num_workers=num_workers,
            initial_num_trees=initial_num_trees,
            final_num_trees=final_num_trees)

        print 'CV  test-ndcg@10: %.4f' % (tune_results['metrics']['cv-test'])
        print 'CV train-ndcg@10: %.4f' % (tune_results['metrics']['cv-train'])

        tune_results['metadata'] = {
            'wiki': wiki,
            'input_dir': input_dir,
            'training_datetime': datetime.datetime.now().isoformat(),
            'num_observations': data_size,
            'num_queries': df_hits_with_features.select('query').drop_duplicates().count(),
            'num_norm_queries': df_hits_with_features.select('norm_query_id').drop_duplicates().count(),
            'features': df_hits_with_features.schema['features'].metadata['features'],
            'summary': summarize_training_df(df_hits_with_features, data_size)
        }

        # Train a model over all data with best params. Use a copy
        # so j_groups doesn't end up inside tune_results and prevent
        # pickle from serializing it.
        best_params = tune_results['params'].copy()
        print 'Best parameters:'
        for param, value in best_params.items():
            print '\t%20s: %s' % (param, value)
        df_grouped, j_groups = mjolnir.training.xgboost.prep_training(
            df_hits_with_features, num_workers)
        best_params['groupData'] = j_groups
        model = mjolnir.training.xgboost.train(df_grouped, best_params)

        tune_results['metrics']['train'] = model.eval(df_grouped, j_groups)
        df_grouped.unpersist()
        print 'train-ndcg@10: %.5f' % (tune_results['metrics']['train'])

        if test_dir is not None:
            try:
                df_test = sqlContext.read.parquet(test_dir)
                tune_results['metrics']['test'] = model.eval(df_test)
                print 'test-ndcg@10: %.5f' % (tune_results['metrics']['test'])
            except:  # noqa: E722
                # It has probably taken some time to get this far. Don't bail
                # because the user input an invalid test dir.
                logging.exception('Could not evaluate test_dir: %s' % (test_dir))

        # Save the tune results somewhere for later analysis. Use pickle
        # to maintain the hyperopt.Trials objects as is. It might be nice
        # to write out a json version, but the Trials objects require
        # some more work before they can be json encoded.
        tune_output_pickle = os.path.join(output_dir, 'tune_%s.pickle' % (wiki))
        with open(tune_output_pickle, 'w') as f:
            # TODO: This includes special hyperopt and mjolnir objects, it would
            # be nice if those could be converted to something simple like dicts
            # and output json instead of pickle. This would greatly simplify
            # post-processing.
            f.write(pickle.dumps(tune_results))
            print 'Wrote tuning results to %s' % (tune_output_pickle)

        # Generate a feature map so xgboost can include feature names in the dump.
        # The final `q` indicates all features are quantitative values (floats).
        features = df_hits_with_features.schema['features'].metadata['features']
        feat_map = ["%d %s q" % (i, fname) for i, fname in enumerate(features)]
        json_model_output = os.path.join(output_dir, 'model_%s.json' % (wiki))
        with open(json_model_output, 'wb') as f:
            f.write(model.dump("\n".join(feat_map)))
            print 'Wrote xgboost json model to %s' % (json_model_output)
        # Write out the xgboost binary format as well, so it can be re-loaded
        # and evaluated
        xgb_model_output = os.path.join(output_dir, 'model_%s.xgb' % (wiki))
        model.saveModelAsLocalFile(xgb_model_output)
        print 'Wrote xgboost binary model to %s' % (xgb_model_output)
        print ''


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description='Train XGBoost ranking models')
    parser.add_argument(
        '-i', '--input', dest='input_dir', type=str, required=True,
        help='Input path, prefixed with hdfs://, to dataframe with labels and features')
    parser.add_argument(
        '-o', '--output', dest='output_dir', type=str, required=True,
        help='Path, on local filesystem, to directory to store the results of '
             'model training to.')
    parser.add_argument(
        '-w', '--workers', dest='num_workers', default=10, type=int,
        help='Number of workers to train each individual model with. The total number '
             + 'of executors required is workers * cv-jobs. (Default: 10)')
    parser.add_argument(
        '-c', '--cv-jobs', dest='num_cv_jobs', default=None, type=int,
        help='Number of cross validation folds to perform in parallel. Defaults to number '
             + 'of folds, to run all in parallel. If this is a multiple of the number '
             + 'of folds multiple cross validations will run in parallel.')
    parser.add_argument(
        '-f', '--folds', dest='num_folds', default=5, type=int,
        help='Number of cross validation folds to use. (Default: 5)')
    parser.add_argument(
        '--initial-trees', dest='initial_num_trees', default=100, type=int,
        help='Number of trees to perform hyperparamter tuning with.  (Default: 100)')
    parser.add_argument(
        '--final-trees', dest='final_num_trees', default=None, type=int,
        help='Number of trees in the final ensemble. If not provided the value from '
             + '--initial-trees will be used.  (Default: None)')
    parser.add_argument(
        '-t', '--test-path', dest='test_dir', type=str, required=False, default=None,
        help='A holdout test set to evaluate the final model against')
    parser.add_argument(
        '-z', '--zero-feature', dest='zero_features', type=str, nargs='+',
        help='Zero out feature in input')
    parser.add_argument(
        '-v', '--verbose', dest='verbose', default=False, action='store_true',
        help='Increase logging to INFO')
    parser.add_argument(
        '-vv', '--very-verbose', dest='very_verbose', default=False, action='store_true',
        help='Increase logging to DEBUG')
    parser.add_argument(
        'wikis', metavar='wiki', type=str, nargs='+',
        help='A wiki to perform model training for.')

    args = parser.parse_args(argv)
    if args.num_cv_jobs is None:
        args.num_cv_jobs = args.num_folds
    return dict(vars(args))


def main(argv=None):
    args = parse_arguments(argv)
    if args['very_verbose']:
        logging.basicConfig(level=logging.DEBUG)
    elif args['verbose']:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig()
    del args['verbose']
    del args['very_verbose']
    # TODO: Set spark configuration? Some can't actually be set here though, so best might be to set all of it
    # on the command line for consistency.
    sc = SparkContext(appName="MLR: training pipeline")
    sc.setLogLevel('WARN')
    sqlContext = HiveContext(sc)

    output_dir = args['output_dir']
    if os.path.exists(output_dir):
        logging.error('Output directory (%s) already exists' % (output_dir))
        sys.exit(1)

    # Maybe this is a bit early to create the path ... but should be fine.
    # The annoyance might be that an error in training requires deleting
    # this directory to try again.
    os.mkdir(output_dir)

    try:
        run_pipeline(sc, sqlContext, **args)
    except:  # noqa: E722
        # If the directory we created is still empty delete it
        # so it doesn't need to be manually re-created
        if not len(glob.glob(os.path.join(output_dir, '*'))):
            os.rmdir(output_dir)
        raise


if __name__ == "__main__":
    main()