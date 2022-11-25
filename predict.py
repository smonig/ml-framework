import os
import yaml
import gc
import hydra
from hydra.utils import to_absolute_path
from omegaconf import OmegaConf, DictConfig

import ROOT as R
import uproot
import pandas as pd
import mlflow

from utils.processing import fill_placeholders, read_hdf
from utils.inference import load_models, predict_folds

@hydra.main(config_path="configs")
def main(cfg: DictConfig) -> None:
    # fill placeholders in the cfg parameters
    input_path = to_absolute_path(cfg["output_path"])
    run_folder = to_absolute_path(f'mlruns/{cfg["experiment_id"]}/{cfg["run_id"]}/')
    models, n_splits, xtrain_split_feature = load_models(run_folder)

    # extract names of training features from mlflow-stored model
    # note: not checking that the set of features is the same across models
    misc_features = OmegaConf.to_object(cfg["misc_features"])
    train_features = []
    with open(to_absolute_path(f'{run_folder}/artifacts/model_0/MLmodel'), 'r') as f:
        model_cfg = yaml.safe_load(f)
    for s in model_cfg['signature']['inputs'].split('{')[1:]:
        train_features.append(s.split("\"")[3])
    fold_id_column = 'fold_id'

    mlflow.set_tracking_uri(f"file://{to_absolute_path('mlruns')}")
    with mlflow.start_run(experiment_id=cfg["experiment_id"], run_id=cfg["run_id"]):
        # loop over input samples
        for sample in cfg["input_samples"]:
            sample_name = list(sample.keys())[0]
            print(f'\n--> Predicting {sample_name}')
            print(f"        loading data set")
            input_filename = fill_placeholders(f'{cfg["output_filename_template_pred"]}.h5', {'{sample_name}': sample_name})

            # read DataFrame from input file
            df = read_hdf(f'{input_path}/{input_filename}', key_list=['cont_features', 'cat_features', 'misc_features', 'targets'])
            df[fold_id_column] = (df[xtrain_split_feature] % n_splits).astype('int32')

            # run cross-inference for folds
            pred_dict = predict_folds(df, train_features, misc_features, fold_id_column=fold_id_column, models=models)

            print(f"        storing to output file")
            output_filename = fill_placeholders(cfg["output_filename_template_pred"], {'{sample_name}': sample_name})
            if cfg["output_format"] == 'root':
                output_filename = '%s.root'%output_filename
                # extract original index
                orig_filename = fill_placeholders(to_absolute_path(f'{cfg["input_path"]}/{cfg["input_filename_template"]}'), {'{sample_name}': sample_name})
                with uproot.open(orig_filename) as f:
                    t = f[cfg['input_tree_name']]
                    orig_index = t.arrays(['evt', 'run'], library='pd')
                    orig_index = list(map(tuple, orig_index.values))
                
                # reorder entries to match original indices
                df_pred = pd.DataFrame(pred_dict).set_index(['evt', 'run'])
                df_pred = df_pred.loc[orig_index].reset_index()
                pred_dict = {c: df_pred[c].values for c in df_pred.columns}

                # store predictions in RDataFrame and snapshot it into output ROOT file
                R_df = R.RDF.MakeNumpyDataFrame(pred_dict)
                R_df.Snapshot(cfg["output_tree_name_pred"], output_filename)
                mlflow.log_artifact(output_filename, artifact_path='pred')
                del(df, R_df); gc.collect()
            elif cfg["output_format"] == 'csv':
                output_filename = '%s.csv'%output_filename
                df_pred = pd.DataFrame(pred_dict)
                df_pred.to_csv(output_filename, index=False)
                mlflow.log_artifact(output_filename, artifact_path='pred')
                del(df_pred); os.remove(output_filename); gc.collect()
            else:
                raise Exception(f'Unknown kind for prediction: {cfg["output_format"]}: should be root or csv')
        print()
        
if __name__ == '__main__':
    main()
