# coding=utf-8
# Copyright 2020 The TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""DatasetBuilder base class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import functools
import inspect
import itertools
import os
import sys

from absl import logging
import six
import tensorflow.compat.v2 as tf

from tensorflow_datasets.core import api_utils
from tensorflow_datasets.core import constants
from tensorflow_datasets.core import download
from tensorflow_datasets.core import lazy_imports_lib
from tensorflow_datasets.core import naming
from tensorflow_datasets.core import registered
from tensorflow_datasets.core import splits as splits_lib
from tensorflow_datasets.core import tfrecords_reader
from tensorflow_datasets.core import tfrecords_writer
from tensorflow_datasets.core import units
from tensorflow_datasets.core import utils
from tensorflow_datasets.core.utils import gcs_utils
from tensorflow_datasets.core.utils import read_config as read_config_lib

import termcolor


FORCE_REDOWNLOAD = download.GenerateMode.FORCE_REDOWNLOAD
REUSE_CACHE_IF_EXISTS = download.GenerateMode.REUSE_CACHE_IF_EXISTS
REUSE_DATASET_IF_EXISTS = download.GenerateMode.REUSE_DATASET_IF_EXISTS

GCS_HOSTED_MSG = """\
Dataset %s is hosted on GCS. It will automatically be downloaded to your
local data directory. If you'd instead prefer to read directly from our public
GCS bucket (recommended if you're running on GCP), you can instead pass
`try_gcs=True` to `tfds.load` or set `data_dir=gs://tfds-data/datasets`.
"""


# Some tests are still running under Python 2, so internally we still whitelist
# Py2 temporary.
_is_py2_download_and_prepare_disabled = True


class BuilderConfig(object):
  """Base class for `DatasetBuilder` data configuration.

  DatasetBuilder subclasses with data configuration options should subclass
  `BuilderConfig` and add their own properties.
  """

  @api_utils.disallow_positional_args
  def __init__(self, name, version=None, supported_versions=None,
               description=None):
    self._name = name
    self._version = version
    self._supported_versions = supported_versions or []
    self._description = description

  @property
  def name(self):
    return self._name

  @property
  def version(self):
    return self._version

  @property
  def supported_versions(self):
    return self._supported_versions

  @property
  def description(self):
    return self._description

  def __repr__(self):
    return "<{cls_name} name={name}, version={version}>".format(
        cls_name=type(self).__name__,
        name=self.name,
        version=self.version or "None")


@six.add_metaclass(registered.RegisteredDataset)
class DatasetBuilder(object):
  """Abstract base class for all datasets.

  `DatasetBuilder` has 3 key methods:

    * `tfds.DatasetBuilder.info`: documents the dataset, including feature
      names, types, and shapes, version, splits, citation, etc.
    * `tfds.DatasetBuilder.download_and_prepare`: downloads the source data
      and writes it to disk.
    * `tfds.DatasetBuilder.as_dataset`: builds an input pipeline using
      `tf.data.Dataset`s.

  **Configuration**: Some `DatasetBuilder`s expose multiple variants of the
  dataset by defining a `tfds.core.BuilderConfig` subclass and accepting a
  config object (or name) on construction. Configurable datasets expose a
  pre-defined set of configurations in `tfds.DatasetBuilder.builder_configs`.

  Typical `DatasetBuilder` usage:

  ```python
  mnist_builder = tfds.builder("mnist")
  mnist_info = mnist_builder.info
  mnist_builder.download_and_prepare()
  datasets = mnist_builder.as_dataset()

  train_dataset, test_dataset = datasets["train"], datasets["test"]
  assert isinstance(train_dataset, tf.data.Dataset)

  # And then the rest of your input pipeline
  train_dataset = train_dataset.repeat().shuffle(1024).batch(128)
  train_dataset = train_dataset.prefetch(2)
  features = tf.compat.v1.data.make_one_shot_iterator(train_dataset).get_next()
  image, label = features['image'], features['label']
  ```
  """

  # Name of the dataset, filled by metaclass based on class name.
  name = None

  # Semantic version of the dataset (ex: tfds.core.Version('1.2.0'))
  VERSION = None

  # List dataset versions which can be loaded using current code.
  # Data can only be prepared with canonical VERSION or above.
  SUPPORTED_VERSIONS = []

  # Named configurations that modify the data generated by download_and_prepare.
  BUILDER_CONFIGS = []

  # Set to True for datasets that are under active development and should not
  # be available through tfds.{load, builder} or documented in overview.md.
  IN_DEVELOPMENT = False

  # Must be set for datasets that use 'manual_dir' functionality - the ones
  # that require users to do additional steps to download the data
  # (this is usually due to some external regulations / rules).
  #
  # This field should contain a string with user instructions, including
  # the list of files that should be present. It will be
  # displayed in the dataset documentation.
  MANUAL_DOWNLOAD_INSTRUCTIONS = None


  @api_utils.disallow_positional_args
  def __init__(self, data_dir=None, config=None, version=None):
    """Constructs a DatasetBuilder.

    Callers must pass arguments as keyword arguments.

    Args:
      data_dir: `str`, directory to read/write data. Defaults to the value of
        the environment variable TFDS_DATA_DIR, if set, otherwise falls back to
        "~/tensorflow_datasets".
      config: `tfds.core.BuilderConfig` or `str` name, optional configuration
        for the dataset that affects the data generated on disk. Different
        `builder_config`s will have their own subdirectories and versions.
      version: `str`. Optional version at which to load the dataset. An error is
        raised if specified version cannot be satisfied. Eg: '1.2.3', '1.2.*'.
        The special value "experimental_latest" will use the highest version,
        even if not default. This is not recommended unless you know what you
        are doing, as the version could be broken.
    """
    # For pickling:
    self._original_state = dict(data_dir=data_dir, config=config,
                                version=version)
    # To do the work:
    self._builder_config = self._create_builder_config(config)
    # Extract code version (VERSION or config)
    if not self._builder_config and not self.VERSION:
      raise AssertionError(
          "DatasetBuilder {} does not have a defined version. Please add a "
          "`VERSION = tfds.core.Version('x.y.z')` to the class.".format(
              self.name))
    self._version = self._pick_version(version)
    # Compute the base directory (for download) and dataset/version directory.
    self._data_dir_root, self._data_dir = self._build_data_dir(data_dir)
    if tf.io.gfile.exists(self._data_dir):
      self.info.read_from_directory(self._data_dir)
    else:  # Use the code version (do not restore data)
      self.info.initialize_from_bucket()

    self._code_dir = os.path.dirname(inspect.getfile(self.__class__))

  def __getstate__(self):
    return self._original_state

  def __setstate__(self, state):
    self.__init__(**state)

  @utils.memoized_property
  def canonical_version(self):
    if self._builder_config:
      return self._builder_config.version
    else:
      return self.VERSION

  @utils.memoized_property
  def supported_versions(self):
    if self._builder_config:
      return self._builder_config.supported_versions
    else:
      return self.SUPPORTED_VERSIONS

  @utils.memoized_property
  def versions(self):
    """Versions (canonical + availables), in preference order."""
    return [
        utils.Version(v) if isinstance(v, six.string_types) else v
        for v in [self.canonical_version] + self.supported_versions
    ]

  def _pick_version(self, requested_version):
    """Returns utils.Version instance, or raise AssertionError."""
    if requested_version == "experimental_latest":
      return max(self.versions)
    for version in self.versions:
      if requested_version is None or version.match(requested_version):
        return version
    available_versions = [str(v) for v in self.versions]
    msg = "Dataset {} cannot be loaded at version {}, only: {}.".format(
        self.name, requested_version, ", ".join(available_versions))
    raise AssertionError(msg)

  @property
  def version(self):
    return self._version

  @property
  def data_dir(self):
    return self._data_dir

  @utils.memoized_property
  def info(self):
    """`tfds.core.DatasetInfo` for this builder."""
    # Ensure .info hasn't been called before versioning is set-up
    # Otherwise, backward compatibility cannot be guaranteed as some code will
    # depend on the code version instead of the restored data version
    if not getattr(self, "_version", None):
      # Message for developper creating new dataset. Will trigger if they are
      # using .info in the constructor before calling super().__init__
      raise AssertionError(
          "Info should not been called before version has been defined. "
          "Otherwise, the created .info may not match the info version from "
          "the restored dataset.")
    return self._info()

  @api_utils.disallow_positional_args
  def download_and_prepare(self, download_dir=None, download_config=None):
    """Downloads and prepares dataset for reading.

    Args:
      download_dir: `str`, directory where downloaded files are stored.
        Defaults to "~/tensorflow-datasets/downloads".
      download_config: `tfds.download.DownloadConfig`, further configuration for
        downloading and preparing dataset.

    Raises:
      IOError: if there is not enough disk space available.
    """

    download_config = download_config or download.DownloadConfig()
    data_exists = tf.io.gfile.exists(self._data_dir)
    if data_exists and download_config.download_mode == REUSE_DATASET_IF_EXISTS:
      logging.info("Reusing dataset %s (%s)", self.name, self._data_dir)
      return

    # Disable `download_and_prepare` (internally, we are still
    # allowing Py2 for the `dataset_builder_tests.py` & cie
    if _is_py2_download_and_prepare_disabled and six.PY2:
      raise NotImplementedError(
          "TFDS has dropped `builder.download_and_prepare` support for "
          "Python 2. Please update your code to Python 3.")

    if self.version.tfds_version_to_prepare:
      available_to_prepare = ", ".join(str(v) for v in self.versions
                                       if not v.tfds_version_to_prepare)
      raise AssertionError(
          "The version of the dataset you are trying to use ({}:{}) can only "
          "be generated using TFDS code synced @ {} or earlier. Either sync to "
          "that version of TFDS to first prepare the data or use another "
          "version of the dataset (available for `download_and_prepare`: "
          "{}).".format(
              self.name, self.version, self.version.tfds_version_to_prepare,
              available_to_prepare))

    # Only `cls.VERSION` or `experimental_latest` versions can be generated.
    # Otherwise, users may accidentally generate an old version using the
    # code from newer versions.
    installable_versions = {
        str(v) for v in (self.canonical_version, max(self.versions))
    }
    if str(self.version) not in installable_versions:
      msg = (
          "The version of the dataset you are trying to use ({}) is too "
          "old for this version of TFDS so cannot be generated."
      ).format(self.info.full_name)
      if self.version.tfds_version_to_prepare:
        msg += (
            "{} can only be generated using TFDS code synced @ {} or earlier "
            "Either sync to that version of TFDS to first prepare the data or "
            "use another version of the dataset. "
        ).format(self.version, self.version.tfds_version_to_prepare)
      else:
        msg += (
            "Either sync to a previous version of TFDS to first prepare the "
            "data or use another version of the dataset. "
        )
      msg += "Available for `download_and_prepare`: {}".format(
          list(sorted(installable_versions)))
      raise ValueError(msg)

    # Currently it's not possible to overwrite the data because it would
    # conflict with versioning: If the last version has already been generated,
    # it will always be reloaded and data_dir will be set at construction.
    if data_exists:
      raise ValueError(
          "Trying to overwrite an existing dataset {} at {}. A dataset with "
          "the same version {} already exists. If the dataset has changed, "
          "please update the version number.".format(self.name, self._data_dir,
                                                     self.version))

    logging.info("Generating dataset %s (%s)", self.name, self._data_dir)
    if not utils.has_sufficient_disk_space(
        self.info.dataset_size + self.info.download_size,
        directory=self._data_dir_root):
      raise IOError(
          "Not enough disk space. Needed: {} (download: {}, generated: {})"
          .format(
              units.size_str(self.info.dataset_size + self.info.download_size),
              units.size_str(self.info.download_size),
              units.size_str(self.info.dataset_size),
          ))
    self._log_download_bytes()

    dl_manager = self._make_download_manager(
        download_dir=download_dir,
        download_config=download_config)

    # Create a tmp dir and rename to self._data_dir on successful exit.
    with utils.incomplete_dir(self._data_dir) as tmp_data_dir:
      # Temporarily assign _data_dir to tmp_data_dir to avoid having to forward
      # it to every sub function.
      with utils.temporary_assignment(self, "_data_dir", tmp_data_dir):
        if (download_config.try_download_gcs and
            gcs_utils.is_dataset_on_gcs(self.info.full_name)):
          logging.warning(GCS_HOSTED_MSG, self.name)
          gcs_utils.download_gcs_dataset(self.info.full_name, self._data_dir)
          self.info.read_from_directory(self._data_dir)
        else:
          self._download_and_prepare(
              dl_manager=dl_manager,
              download_config=download_config)

          # NOTE: If modifying the lines below to put additional information in
          # DatasetInfo, you'll likely also want to update
          # DatasetInfo.read_from_directory to possibly restore these attributes
          # when reading from package data.

          # Skip statistics computation if tfdv isn't present
          try:
            import tensorflow_data_validation  # pylint: disable=g-import-not-at-top,import-outside-toplevel,unused-import  # pytype: disable=import-error
            skip_stats_computation = False
          except ImportError:
            skip_stats_computation = True

          splits = list(self.info.splits.values())
          statistics_already_computed = bool(
              splits and splits[0].statistics.num_examples)
          # Update DatasetInfo metadata by computing statistics from the data.
          if (skip_stats_computation or
              download_config.compute_stats == download.ComputeStatsMode.SKIP or
              download_config.compute_stats == download.ComputeStatsMode.AUTO
              and statistics_already_computed
             ):
            logging.info(
                "Skipping computing stats for mode %s.",
                download_config.compute_stats)
          else:  # Mode is forced or stats do not exists yet
            logging.info("Computing statistics.")
            self.info.compute_dynamic_properties()
          self.info.download_size = dl_manager.downloaded_size
          # Write DatasetInfo to disk, even if we haven't computed statistics.
          self.info.write_to_directory(self._data_dir)
    self._log_download_done()

  @api_utils.disallow_positional_args
  def as_dataset(
      self,
      split=None,
      batch_size=None,
      shuffle_files=False,
      decoders=None,
      read_config=None,
      as_supervised=False,
  ):
    # pylint: disable=line-too-long
    """Constructs a `tf.data.Dataset`.

    Callers must pass arguments as keyword arguments.

    The output types vary depending on the parameters. Examples:

    ```python
    builder = tfds.builder('imdb_reviews')
    builder.download_and_prepare()

    # Default parameters: Returns the dict of tf.data.Dataset
    ds_all_dict = builder.as_dataset()
    assert isinstance(ds_all_dict, dict)
    print(ds_all_dict.keys())  # ==> ['test', 'train', 'unsupervised']

    assert isinstance(ds_all_dict['test'], tf.data.Dataset)
    # Each dataset (test, train, unsup.) consists of dictionaries
    # {'label': <tf.Tensor: .. dtype=int64, numpy=1>,
    #  'text': <tf.Tensor: .. dtype=string, numpy=b"I've watched the movie ..">}
    # {'label': <tf.Tensor: .. dtype=int64, numpy=1>,
    #  'text': <tf.Tensor: .. dtype=string, numpy=b'If you love Japanese ..'>}

    # With as_supervised: tf.data.Dataset only contains (feature, label) tuples
    ds_all_supervised = builder.as_dataset(as_supervised=True)
    assert isinstance(ds_all_supervised, dict)
    print(ds_all_supervised.keys())  # ==> ['test', 'train', 'unsupervised']

    assert isinstance(ds_all_supervised['test'], tf.data.Dataset)
    # Each dataset (test, train, unsup.) consists of tuples (text, label)
    # (<tf.Tensor: ... dtype=string, numpy=b"I've watched the movie ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)
    # (<tf.Tensor: ... dtype=string, numpy=b"If you love Japanese ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)

    # Same as above plus requesting a particular split
    ds_test_supervised = builder.as_dataset(as_supervised=True, split='test')
    assert isinstance(ds_test_supervised, tf.data.Dataset)
    # The dataset consists of tuples (text, label)
    # (<tf.Tensor: ... dtype=string, numpy=b"I've watched the movie ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)
    # (<tf.Tensor: ... dtype=string, numpy=b"If you love Japanese ..">,
    #  <tf.Tensor: ... dtype=int64, numpy=1>)
    ```

    Args:
      split: Which split of the data to load (e.g. `'train'`, `'test'`
        `['train', 'test']`, `'train[80%:]'`,...). See our
        [split API guide](https://www.tensorflow.org/datasets/splits).
        If `None`, will return all splits in a `Dict[Split, tf.data.Dataset]`.
      batch_size: `int`, batch size. Note that variable-length features will
        be 0-padded if `batch_size` is set. Users that want more custom behavior
        should use `batch_size=None` and use the `tf.data` API to construct a
        custom pipeline. If `batch_size == -1`, will return feature
        dictionaries of the whole dataset with `tf.Tensor`s instead of a
        `tf.data.Dataset`.
      shuffle_files: `bool`, whether to shuffle the input files. Defaults to
        `False`.
      decoders: Nested dict of `Decoder` objects which allow to customize the
        decoding. The structure should match the feature structure, but only
        customized feature keys need to be present. See
        [the guide](https://github.com/tensorflow/datasets/tree/master/docs/decode.md)
        for more info.
      read_config: `tfds.ReadConfig`, Additional options to configure the
        input pipeline (e.g. seed, num parallel reads,...).
      as_supervised: `bool`, if `True`, the returned `tf.data.Dataset`
        will have a 2-tuple structure `(input, label)` according to
        `builder.info.supervised_keys`. If `False`, the default,
        the returned `tf.data.Dataset` will have a dictionary with all the
        features.

    Returns:
      `tf.data.Dataset`, or if `split=None`, `dict<key: tfds.Split, value:
      tfds.data.Dataset>`.

      If `batch_size` is -1, will return feature dictionaries containing
      the entire dataset in `tf.Tensor`s instead of a `tf.data.Dataset`.
    """
    # pylint: enable=line-too-long
    logging.info("Constructing tf.data.Dataset for split %s, from %s",
                 split, self._data_dir)
    if not tf.io.gfile.exists(self._data_dir):
      raise AssertionError(
          ("Dataset %s: could not find data in %s. Please make sure to call "
           "dataset_builder.download_and_prepare(), or pass download=True to "
           "tfds.load() before trying to access the tf.data.Dataset object."
          ) % (self.name, self._data_dir_root))

    # By default, return all splits
    if split is None:
      split = {s: s for s in self.info.splits}

    read_config = read_config or read_config_lib.ReadConfig()

    # Create a dataset for each of the given splits
    build_single_dataset = functools.partial(
        self._build_single_dataset,
        shuffle_files=shuffle_files,
        batch_size=batch_size,
        decoders=decoders,
        read_config=read_config,
        as_supervised=as_supervised,
    )
    datasets = utils.map_nested(build_single_dataset, split, map_tuple=True)
    return datasets

  def _build_single_dataset(
      self,
      split,
      shuffle_files,
      batch_size,
      decoders,
      read_config,
      as_supervised,
  ):
    """as_dataset for a single split."""
    wants_full_dataset = batch_size == -1
    if wants_full_dataset:
      batch_size = self.info.splits.total_num_examples or sys.maxsize

    # Build base dataset
    ds = self._as_dataset(
        split=split,
        shuffle_files=shuffle_files,
        decoders=decoders,
        read_config=read_config,
    )
    # Auto-cache small datasets which are small enough to fit in memory.
    if self._should_cache_ds(
        split=split,
        shuffle_files=shuffle_files,
        read_config=read_config
    ):
      ds = ds.cache()

    if batch_size:
      # Use padded_batch so that features with unknown shape are supported.
      ds = ds.padded_batch(
          batch_size, tf.compat.v1.data.get_output_shapes(ds))

    if as_supervised:
      if not self.info.supervised_keys:
        raise ValueError(
            "as_supervised=True but %s does not support a supervised "
            "(input, label) structure." % self.name)
      input_f, target_f = self.info.supervised_keys
      ds = ds.map(lambda fs: (fs[input_f], fs[target_f]),
                  num_parallel_calls=tf.data.experimental.AUTOTUNE)

    ds = ds.prefetch(tf.data.experimental.AUTOTUNE)

    # If shuffling is True and seeds not set, allow pipeline to be
    # non-deterministic
    # This code should probably be moved inside tfreader, such as
    # all the tf.data.Options are centralized in a single place.
    if (shuffle_files and
        read_config.options.experimental_deterministic is None and
        read_config.shuffle_seed is None):
      options = tf.data.Options()
      options.experimental_deterministic = False
      ds = ds.with_options(options)
    # If shuffle is False, keep the default value (deterministic), which
    # allow the user to overwritte it.

    if wants_full_dataset:
      return tf.data.experimental.get_single_element(ds)
    return ds

  def _should_cache_ds(self, split, shuffle_files, read_config):
    """Returns True if TFDS should auto-cache the dataset."""
    # The user can explicitly opt-out from auto-caching
    if not read_config.try_autocache:
      return False

    # Skip datasets with unknown size.
    # Even by using heuristic with `download_size` and
    # `MANUAL_DOWNLOAD_INSTRUCTIONS`, it wouldn't catch datasets which hardcode
    # the non-processed data-dir, nor DatasetBuilder not based on tf-record.
    if not self.info.dataset_size:
      return False

    # Do not cache big datasets
    # Instead of using the global size, we could infer the requested bytes:
    # `self.info.splits[split].num_bytes`
    # The info is available for full splits, and could be approximated
    # for subsplits `train[:50%]`.
    # However if the user is creating multiple small splits from a big
    # dataset, those could adds up and fill up the entire RAM.
    # 250 MiB is arbitrary picked. For comparison, Cifar10 is about 150 MiB.
    if self.info.dataset_size > 250 * units.MiB:
      return False

    # We do not want to cache data which has more than one shards when
    # shuffling is enabled, as this would effectivelly disable shuffling.
    # An exception is for single shard (as shuffling is a no-op).
    # Another exception is if reshuffle is disabled (shuffling already cached)
    num_shards = len(self.info.splits[split].file_instructions)
    if (shuffle_files and
        # Shuffling only matter when reshuffle is True or None (default)
        read_config.shuffle_reshuffle_each_iteration is not False and  # pylint: disable=g-bool-id-comparison
        num_shards > 1):
      return False

    # If the dataset satisfy all the right conditions, activate autocaching.
    return True

  def _relative_data_dir(self, with_version=True):
    """Relative path of this dataset in data_dir."""
    builder_data_dir = self.name
    builder_config = self._builder_config
    if builder_config:
      builder_data_dir = os.path.join(builder_data_dir, builder_config.name)
    if not with_version:
      return builder_data_dir

    version_data_dir = os.path.join(builder_data_dir, str(self._version))
    return version_data_dir

  def _build_data_dir(self, given_data_dir):
    """Return the data directory for the current version.

    Args:
      given_data_dir: `Optional[str]`, root `data_dir` passed as
        `__init__` argument.

    Returns:
      data_dir_root: `str`, The root dir containing all datasets, downloads,...
      data_dir: `str`, The version data_dir
        (e.g. `<data_dir_root>/<ds_name>/<config>/<version>`)
    """
    builder_dir = self._relative_data_dir(with_version=False)
    version_dir = self._relative_data_dir(with_version=True)

    # If the data dir is explicitly given, no need to search everywhere.
    if given_data_dir:
      default_data_dir = os.path.expanduser(given_data_dir)
      all_data_dirs = [default_data_dir]
    else:
      default_data_dir = os.path.expanduser(constants.DATA_DIR)
      all_data_dirs = constants.list_data_dirs()

    all_version_dirs = set()
    requested_version_dirs = {}
    for data_dir_root in all_data_dirs:
      # List all existing versions
      all_version_dirs.update(
          _list_all_version_dirs(os.path.join(data_dir_root, builder_dir)))
      # Check for existance of the requested dir
      requested_version_dir = os.path.join(data_dir_root, version_dir)
      if requested_version_dir in all_version_dirs:
        requested_version_dirs[data_dir_root] = requested_version_dir

    if len(requested_version_dirs) > 1:
      raise ValueError(
          "Dataset was found in more than one directory: {}. Please resolve "
          "the ambiguity by explicitly specifying `data_dir=`."
          "".format(requested_version_dirs))
    elif len(requested_version_dirs) == 1:  # The dataset is found once
      return next(iter(requested_version_dirs.items()))

    # No dataset found, use default directory
    data_dir = os.path.join(default_data_dir, version_dir)
    if all_version_dirs:
      logging.warning(
          "Found a different version of the requested dataset:\n"
          "%s\n"
          "Using %s instead.",
          "\n".join(sorted(all_version_dirs)),
          data_dir
      )
    return default_data_dir, data_dir

  def _log_download_done(self):
    msg = ("Dataset {name} downloaded and prepared to {data_dir}. "
           "Subsequent calls will reuse this data.").format(
               name=self.name,
               data_dir=self._data_dir,
           )
    termcolor.cprint(msg, attrs=["bold"])

  def _log_download_bytes(self):
    # Print is intentional: we want this to always go to stdout so user has
    # information needed to cancel download/preparation if needed.
    # This comes right before the progress bar.
    termcolor.cprint(
        "Downloading and preparing dataset {} (download: {}, generated: {}, "
        "total: {}) to {}...".format(
            self.info.full_name,
            units.size_str(self.info.download_size),
            units.size_str(self.info.dataset_size),
            units.size_str(self.info.download_size + self.info.dataset_size),
            self._data_dir,
        ), attrs=["bold"])

  @abc.abstractmethod
  def _info(self):
    """Construct the DatasetInfo object. See `DatasetInfo` for details.

    Warning: This function is only called once and the result is cached for all
    following .info() calls.

    Returns:
      dataset_info: (DatasetInfo) The dataset information
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _download_and_prepare(self, dl_manager, download_config=None):
    """Downloads and prepares dataset for reading.

    This is the internal implementation to overwrite called when user calls
    `download_and_prepare`. It should download all required data and generate
    the pre-processed datasets files.

    Args:
      dl_manager: (DownloadManager) `DownloadManager` used to download and cache
        data.
      download_config: `DownloadConfig`, Additional options.
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _as_dataset(
      self, split, decoders=None, read_config=None, shuffle_files=False):
    """Constructs a `tf.data.Dataset`.

    This is the internal implementation to overwrite called when user calls
    `as_dataset`. It should read the pre-processed datasets files and generate
    the `tf.data.Dataset` object.

    Args:
      split: `tfds.Split` which subset of the data to read.
      decoders: Nested structure of `Decoder` object to customize the dataset
        decoding.
      read_config: `tfds.ReadConfig`
      shuffle_files: `bool`, whether to shuffle the input files. Optional,
        defaults to `False`.

    Returns:
      `tf.data.Dataset`
    """
    raise NotImplementedError

  def _make_download_manager(self, download_dir, download_config):
    """Creates a new download manager object."""
    download_dir = download_dir or os.path.join(self._data_dir_root,
                                                "downloads")
    extract_dir = (download_config.extract_dir or
                   os.path.join(download_dir, "extracted"))
    checksums_path = os.path.join(self._code_dir, "checksums.txt")

    # Use manual_dir only if MANUAL_DOWNLOAD_INSTRUCTIONS are set.
    if self.MANUAL_DOWNLOAD_INSTRUCTIONS:
      manual_dir = (
          download_config.manual_dir or os.path.join(download_dir, "manual"))
    else:
      manual_dir = None

    return download.DownloadManager(
        dataset_name=self.name,
        download_dir=download_dir,
        extract_dir=extract_dir,
        manual_dir=manual_dir,
        checksums_path=checksums_path,
        manual_dir_instructions=utils.dedent(self.MANUAL_DOWNLOAD_INSTRUCTIONS),
        force_download=(download_config.download_mode == FORCE_REDOWNLOAD),
        force_extraction=(download_config.download_mode == FORCE_REDOWNLOAD),
        force_checksums_validation=download_config.force_checksums_validation,
        register_checksums=download_config.register_checksums,
    )

  @property
  def builder_config(self):
    """`tfds.core.BuilderConfig` for this builder."""
    return self._builder_config

  def _create_builder_config(self, builder_config):
    """Create and validate BuilderConfig object."""
    if builder_config is None and self.BUILDER_CONFIGS:
      builder_config = self.BUILDER_CONFIGS[0]
      logging.info("No config specified, defaulting to first: %s/%s", self.name,
                   builder_config.name)
    if not builder_config:
      return None
    if isinstance(builder_config, six.string_types):
      name = builder_config
      builder_config = self.builder_configs.get(name)
      if builder_config is None:
        raise ValueError("BuilderConfig %s not found. Available: %s" %
                         (name, list(self.builder_configs.keys())))
    name = builder_config.name
    if not name:
      raise ValueError("BuilderConfig must have a name, got %s" % name)
    is_custom = name not in self.builder_configs
    if is_custom:
      logging.warning("Using custom data configuration %s", name)
    else:
      if builder_config is not self.builder_configs[name]:
        raise ValueError(
            "Cannot name a custom BuilderConfig the same as an available "
            "BuilderConfig. Change the name. Available BuilderConfigs: %s" %
            (list(self.builder_configs.keys())))
      if not builder_config.version:
        raise ValueError("BuilderConfig %s must have a version" % name)
      if not builder_config.description:
        raise ValueError("BuilderConfig %s must have a description" % name)
    return builder_config

  @utils.classproperty
  @classmethod
  @utils.memoize()
  def builder_configs(cls):
    """Pre-defined list of configurations for this builder class."""
    config_dict = {config.name: config for config in cls.BUILDER_CONFIGS}
    if len(config_dict) != len(cls.BUILDER_CONFIGS):
      names = [config.name for config in cls.BUILDER_CONFIGS]
      raise ValueError(
          "Names in BUILDER_CONFIGS must not be duplicated. Got %s" % names)
    return config_dict


def _list_all_version_dirs(root_dir):
  """Lists all dataset versions present on disk."""
  if not tf.io.gfile.exists(root_dir):
    return []

  def _is_version_valid(version):
    try:
      return utils.Version(version) and True
    except ValueError:  # Invalid version (ex: incomplete data dir)
      return False

  return [  # Return all version dirs
      os.path.join(root_dir, version)
      for version in tf.io.gfile.listdir(root_dir)
      if _is_version_valid(version)
  ]


class FileAdapterBuilder(DatasetBuilder):
  """Base class for datasets with data generation based on file adapter."""

  @utils.memoized_property
  def _example_specs(self):
    return self.info.features.get_serialized_info()

  @property
  def _tfrecords_reader(self):
    return tfrecords_reader.Reader(self._data_dir, self._example_specs)

  @abc.abstractmethod
  def _split_generators(self, dl_manager):
    """Specify feature dictionary generators and dataset splits.

    This function returns a list of `SplitGenerator`s defining how to generate
    data and what splits to use.

    Example:

      return[
          tfds.core.SplitGenerator(
              name=tfds.Split.TRAIN,
              gen_kwargs={'file': 'train_data.zip'},
          ),
          tfds.core.SplitGenerator(
              name=tfds.Split.TEST,
              gen_kwargs={'file': 'test_data.zip'},
          ),
      ]

    The above code will first call `_generate_examples(file='train_data.zip')`
    to write the train data, then `_generate_examples(file='test_data.zip')` to
    write the test data.

    Datasets are typically split into different subsets to be used at various
    stages of training and evaluation.

    Note that for datasets without a `VALIDATION` split, you can use a
    fraction of the `TRAIN` data for evaluation as you iterate on your model
    so as not to overfit to the `TEST` data.

    For downloads and extractions, use the given `download_manager`.
    Note that the `DownloadManager` caches downloads, so it is fine to have each
    generator attempt to download the source data.

    A good practice is to download all data in this function, and then
    distribute the relevant parts to each split with the `gen_kwargs` argument

    Args:
      dl_manager: (DownloadManager) Download manager to download the data

    Returns:
      `list<SplitGenerator>`.
    """
    raise NotImplementedError()

  @abc.abstractmethod
  def _prepare_split(self, split_generator, **kwargs):
    """Generate the examples and record them on disk.

    Args:
      split_generator: `SplitGenerator`, Split generator to process
      **kwargs: Additional kwargs forwarded from _download_and_prepare (ex:
        beam pipeline)
    """
    raise NotImplementedError()

  def _make_split_generators_kwargs(self, prepare_split_kwargs):
    """Get kwargs for `self._split_generators()` from `prepare_split_kwargs`."""
    del prepare_split_kwargs
    return {}

  def _download_and_prepare(self, dl_manager, **prepare_split_kwargs):
    if not tf.io.gfile.exists(self._data_dir):
      tf.io.gfile.makedirs(self._data_dir)

    # Generating data for all splits
    split_dict = splits_lib.SplitDict(dataset_name=self.name)
    split_generators_kwargs = self._make_split_generators_kwargs(
        prepare_split_kwargs)
    for split_generator in self._split_generators(
        dl_manager, **split_generators_kwargs):
      if str(split_generator.split_info.name).lower() == "all":
        raise ValueError(
            "`all` is a special split keyword corresponding to the "
            "union of all splits, so cannot be used as key in "
            "._split_generator()."
        )

      logging.info("Generating split %s", split_generator.split_info.name)
      split_dict.add(split_generator.split_info)

      # Prepare split will record examples associated to the split
      self._prepare_split(split_generator, **prepare_split_kwargs)

    # Update the info object with the splits.
    self.info.update_splits_if_different(split_dict)

  def _as_dataset(
      self,
      split=splits_lib.Split.TRAIN,
      decoders=None,
      read_config=None,
      shuffle_files=False):
    ds = self._tfrecords_reader.read(
        name=self.name,
        instructions=split,
        split_infos=self.info.splits.values(),
        read_config=read_config,
        shuffle_files=shuffle_files,
    )
    decode_fn = functools.partial(
        self.info.features.decode_example, decoders=decoders)
    ds = ds.map(decode_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    return ds


class GeneratorBasedBuilder(FileAdapterBuilder):
  """Base class for datasets with data generation based on dict generators.

  `GeneratorBasedBuilder` is a convenience class that abstracts away much
  of the data writing and reading of `DatasetBuilder`. It expects subclasses to
  implement generators of feature dictionaries across the dataset splits
  `_split_generators`. See the method docstrings for details.

  """

  @abc.abstractmethod
  def _generate_examples(self, **kwargs):
    """Default function generating examples for each `SplitGenerator`.

    This function preprocess the examples from the raw data to the preprocessed
    dataset files.
    This function is called once for each `SplitGenerator` defined in
    `_split_generators`. The examples yielded here will be written on
    disk.

    Args:
      **kwargs: `dict`, Arguments forwarded from the SplitGenerator.gen_kwargs

    Yields:
      key: `str` or `int`, a unique deterministic example identification key.
        * Unique: An error will be raised if two examples are yield with the
          same key.
        * Deterministic: When generating the dataset twice, the same example
          should have the same key.
        Good keys can be the image id, or line number if examples are extracted
        from a text file.
        The key will be hashed and sorted to shuffle examples deterministically,
        such as generating the dataset multiple times keep examples in the
        same order.
      example: `dict<str feature_name, feature_value>`, a feature dictionary
        ready to be encoded and written to disk. The example will be
        encoded with `self.info.features.encode_example({...})`.
    """
    raise NotImplementedError()

  def _download_and_prepare(self, dl_manager, download_config):
    # Extract max_examples_per_split and forward it to _prepare_split
    super(GeneratorBasedBuilder, self)._download_and_prepare(
        dl_manager=dl_manager,
        max_examples_per_split=download_config.max_examples_per_split,
    )

  def _prepare_split(self, split_generator, max_examples_per_split):
    generator = self._generate_examples(**split_generator.gen_kwargs)
    split_info = split_generator.split_info
    if max_examples_per_split is not None:
      logging.warning("Splits capped at %s examples max.",
                      max_examples_per_split)
      generator = itertools.islice(generator, max_examples_per_split)
    fname = "{}-{}.tfrecord".format(self.name, split_generator.name)
    fpath = os.path.join(self._data_dir, fname)
    writer = tfrecords_writer.Writer(self._example_specs, fpath,
                                     hash_salt=split_generator.name)
    for key, record in utils.tqdm(generator, unit=" examples",
                                  total=split_info.num_examples, leave=False):
      example = self.info.features.encode_example(record)
      writer.write(key, example)
    shard_lengths, total_size = writer.finalize()
    split_generator.split_info.shard_lengths.extend(shard_lengths)
    split_generator.split_info.num_bytes = total_size


class BeamBasedBuilder(FileAdapterBuilder):
  """Beam based Builder."""

  def __init__(self, *args, **kwargs):
    super(BeamBasedBuilder, self).__init__(*args, **kwargs)
    self._beam_writers = {}  # {split: beam_writer} mapping.

  def _make_split_generators_kwargs(self, prepare_split_kwargs):
    # Pass `pipeline` into `_split_generators()` from `prepare_split_kwargs` if
    # it's in the call signature of `_split_generators()`.
    # This allows for global preprocessing in beam.
    split_generators_kwargs = {}
    split_generators_arg_names = (
        inspect.getargspec(self._split_generators).args if six.PY2 else  # pylint: disable=deprecated-method  # pytype: disable=wrong-arg-types
        inspect.signature(self._split_generators).parameters.keys())
    if "pipeline" in split_generators_arg_names:
      split_generators_kwargs["pipeline"] = prepare_split_kwargs["pipeline"]
    return split_generators_kwargs

  @abc.abstractmethod
  def _build_pcollection(self, pipeline, **kwargs):
    """Build the beam pipeline examples for each `SplitGenerator`.

    This function extracts examples from the raw data with parallel transforms
    in a Beam pipeline. It is called once for each `SplitGenerator` defined in
    `_split_generators`. The examples from the PCollection will be
    encoded and written to disk.

    Warning: When running in a distributed setup, make sure that the data
    which will be read (download_dir, manual_dir,...) and written (data_dir)
    can be accessed by the workers jobs. The data should be located in a
    shared filesystem, like GCS.

    Example:

    ```
    def _build_pcollection(pipeline, extracted_dir):
      return (
          pipeline
          | beam.Create(gfile.io.listdir(extracted_dir))
          | beam.Map(_process_file)
      )
    ```

    Args:
      pipeline: `beam.Pipeline`, root Beam pipeline
      **kwargs: Arguments forwarded from the SplitGenerator.gen_kwargs

    Returns:
      pcollection: `PCollection`, an Apache Beam PCollection containing the
        example to send to `self.info.features.encode_example(...)`.
    """
    raise NotImplementedError()

  def _download_and_prepare(self, dl_manager, download_config):
    # Create the Beam pipeline and forward it to _prepare_split
    beam = lazy_imports_lib.lazy_imports.apache_beam

    if not download_config.beam_runner and not download_config.beam_options:
      raise ValueError(
          "The dataset you're trying to generate is using Apache Beam. Beam"
          "datasets are usually very large and should be generated separately."
          "Please have a look at"
          "https://www.tensorflow.org/datasets/beam_datasets#generating_a_beam_dataset"
          "for instructions."
      )

    beam_options = (download_config.beam_options or
                    beam.options.pipeline_options.PipelineOptions())
    # Beam type checking assumes transforms multiple outputs are of same type,
    # which is not our case. Plus it doesn't handle correctly all types, so we
    # are better without it.
    beam_options.view_as(
        beam.options.pipeline_options.TypeOptions).pipeline_type_check = False
    # Use a single pipeline for all splits
    with beam.Pipeline(
        runner=download_config.beam_runner,
        options=beam_options,
    ) as pipeline:
      # TODO(tfds): Should eventually try to add support to
      # download_config.max_examples_per_split
      super(BeamBasedBuilder, self)._download_and_prepare(
          dl_manager,
          pipeline=pipeline,
      )

    # Update `info.splits` with number of shards and shard lengths.
    split_dict = self.info.splits
    for split_name, beam_writer in self._beam_writers.items():
      logging.info("Retrieving shard lengths for %s...", split_name)
      shard_lengths, total_size = beam_writer.finalize()
      split_info = split_dict[split_name]
      split_info.shard_lengths.extend(shard_lengths)
      split_info.num_shards = len(shard_lengths)
      split_info.num_bytes = total_size
    logging.info("Updating split info...")
    self.info.update_splits_if_different(split_dict)

  def _prepare_split(self, split_generator, pipeline):
    beam = lazy_imports_lib.lazy_imports.apache_beam

    if not tf.io.gfile.exists(self._data_dir):
      tf.io.gfile.makedirs(self._data_dir)

    split_name = split_generator.split_info.name
    output_prefix = naming.filename_prefix_for_split(
        self.name, split_name)
    output_prefix = os.path.join(self._data_dir, output_prefix)

    # To write examples to disk:
    fname = "{}-{}.tfrecord".format(self.name, split_name)
    fpath = os.path.join(self._data_dir, fname)
    beam_writer = tfrecords_writer.BeamWriter(
        self._example_specs, fpath, hash_salt=split_name)
    self._beam_writers[split_name] = beam_writer

    encode_example = self.info.features.encode_example

    # Note: We need to wrap the pipeline in a PTransform to avoid re-using the
    # same label names for each split
    @beam.ptransform_fn
    def _build_pcollection(pipeline):
      """PTransformation which build a single split."""
      # Encode the PCollection
      pcoll_examples = self._build_pcollection(
          pipeline, **split_generator.gen_kwargs)
      pcoll_examples |= "Encode" >> beam.Map(
          lambda key_ex: (key_ex[0], encode_example(key_ex[1])))
      return beam_writer.write_from_pcollection(pcoll_examples)

    # Add the PCollection to the pipeline
    _ = pipeline | split_name >> _build_pcollection()   # pylint: disable=no-value-for-parameter
