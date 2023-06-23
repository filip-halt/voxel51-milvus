"""
Milvus similarity backend.

| Copyright 2017-2023, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import logging

import numpy as np
from uuid import uuid4
import eta.core.utils as etau

import fiftyone.core.utils as fou
from fiftyone.brain.similarity import (
    SimilarityConfig,
    Similarity,
    SimilarityIndex,
)

pymilvus = fou.lazy_import("pymilvus")


logger = logging.getLogger(__name__)

_SUPPORTED_METRICS = {
    "dotproduct": "IP",
    "euclidean": "L2",
}

class MilvusSimilarityConfig(SimilarityConfig):
    """Configuration for the Milvus similarity backend.

    Args:
        embeddings_field (None): the sample field containing the embeddings,
            if one was provided.
        model (None): the :class:`fiftyone.core.models.Model` or name of the
            zoo model that was used to compute embeddings, if known.
        patches_field (None): the sample field defining the patches being
            analyzed, if any.
        supports_prompts (None): whether this run supports prompt queries.
        metric (str): the embedding distance metric to use when creating a
            new index. Supported values are.
            ``("dotproduct", "euclidean")``
        collection_name (str): the name of a Milvus collection to use or
            create. If none is provided, a new collection will be created.
        uri (str):  full address of Milvus server.
        user (str): username if using rbac.
        password(str): password for supplied username.
        consistency_level(str): which consistency level to use. Possible values are Strong, Session, Bounded, Eventually.
        overwrite(str): whether to overwrite the collection if it already exists.
    """
    def __init__(
        self,
        embeddings_field=None,
        model=None,
        patches_field=None,
        supports_prompts=None,
        metric="euclidean",
        collection_name: str = "ClientCollection",
        uri: str = "http://localhost:19530",
        user: str = "",
        password: str = "",
        consistency_level: str = "Session",
        **kwargs,
    ):
        if metric is not None and metric not in _SUPPORTED_METRICS:
            raise ValueError(
                "Unsupported metric '%s'. Supported values are %s"
                % (metric, tuple(_SUPPORTED_METRICS.keys()))
            )

        super().__init__(
            embeddings_field=embeddings_field,
            model=model,
            patches_field=patches_field,
            supports_prompts=supports_prompts,
            **kwargs,
        )
        self.metric = metric
        self.collection_name = collection_name
        self._uri = uri
        self.user = user
        self.password = password
        self.consistency_level = consistency_level
        self.index_params = {
            "metric_type": _SUPPORTED_METRICS[metric],
            "index_type": "HNSW",
            "params": {"M": 8, "efConstruction": 64},
        }
        self.search_params = {
            "HNSW": {"metric_type": _SUPPORTED_METRICS[metric], "params": {"ef": 10}},
        }

    @property
    def method(self):
        return "milvus"

    @property
    def uri(self):
        return self._uri

    @uri.setter
    def uri(self, uri):
        self._uri = uri

    @property
    def max_k(self):
        return 16_384

    @property
    def supports_least_similarity(self):
        return False

    @property
    def supported_aggregations(self):
        return ("mean",)

    def load_credentials(self, uri=None):
        self._load_parameters(uri=uri)


class MilvusSimilarity(Similarity):
    """Milvus similarity factory.

    Args:
        config: a :class:`MilvusSimilarityConfig`
    """

    def ensure_requirements(self):
        fou.ensure_package("pymilvus")

    def ensure_usage_requirements(self):
        fou.ensure_package("pymilvus")

    def initialize(self, samples, brain_key):
        return MilvusSimilarityIndex(samples, self.config, brain_key, backend=self)


class MilvusSimilarityIndex(SimilarityIndex):
    """Class for interacting with Milvus similarity indexes.

    Args:
        samples: the :class:`fiftyone.core.collections.SampleCollection` used
        config: the :class:`MilvusSimilarityConfig` used
        brain_key: the brain key
        backend (None): a :class:`MilvusSimilarity` instance
    """

    def __init__(self, samples, config, brain_key, backend=None):
        super().__init__(samples, config, brain_key, backend=backend)
        self._initialize()

    def _initialize(self):
        from pymilvus import utility

        self.alias = self._connect(
            self.config.uri, self.config.user, self.config.password
        )

        self._init_collection()

    def _connect(self, uri, user, password):
        from pymilvus import connections, MilvusException

        """Create the connection to the Milvus server."""
        alias = uuid4().hex
        try:
            connections.connect(alias=alias, uri=uri, user=user, password=password)
            logger.debug("Created new connection using: %s", alias)
            return alias
        except MilvusException as ex:
            logger.error("Failed to create new connection using: %s", alias)
            raise ex

    def _init_collection(self):
        from pymilvus import utility, Collection

        if utility.has_collection(self.config.collection_name, using=self.alias):
            col = Collection(self.config.collection_name, using=self.alias)
            col.load()
            for x in col.schema.fields:
                if x.params.get("dim", None) is not None:
                    dim = x.params["dim"]
                    break
            return (col, dim)

        return None, None

    @property
    def total_index_size(self):
        col = self.get_collection()
        col.flush()
        return col.num_entities

    def add_to_index(
        self,
        embeddings,
        sample_ids,
        label_ids=None,
        overwrite=True,
        allow_existing=True,
        warn_existing=False,
        batch_size=100,
    ):
        from pymilvus import utility

        if not utility.has_collection(self.config.collection_name, using=self.alias):
            self._create_collection(embeddings.shape[1])

        if label_ids is not None:
            ids = label_ids
        else:
            ids = sample_ids

        if warn_existing or not allow_existing or not overwrite:
            existing_ids = self.get_existing_ids(ids)
            num_existing = len(existing_ids)

            if num_existing > 0:
                if not allow_existing:
                    raise ValueError(
                        "Found %d IDs (eg %s) that already exist in the index"
                        % (num_existing, next(iter(existing_ids)))
                    )

                if warn_existing:
                    if overwrite:
                        logger.warning(
                            "Overwriting %d IDs that already exist in the " "index",
                            num_existing,
                        )
                    else:
                        logger.warning(
                            "Skipping %d IDs that already exist in the index",
                            num_existing,
                        )
        else:
            existing_ids = set()

        if existing_ids and not overwrite:
            del_inds = [i for i, _id in enumerate(ids) if _id in existing_ids]
            embeddings = np.delete(embeddings, del_inds)
            sample_ids = np.delete(sample_ids, del_inds)
            if label_ids is not None:
                label_ids = np.delete(label_ids, del_inds)

        elif existing_ids and overwrite:
            self._delete_ids(existing_ids)

        embeddings = [e.tolist() for e in embeddings]
        sample_ids = list(sample_ids)
        ids = list(ids)

        for _embeddings, _ids, _sample_ids in zip(
            fou.iter_batches(embeddings, batch_size),
            fou.iter_batches(ids, batch_size),
            fou.iter_batches(sample_ids, batch_size),
        ):
            insert_data = [
                list(_ids),
                list(_embeddings),
                list(_sample_ids),
            ]
            self.get_collection().insert(insert_data)

    def _create_collection(self, dimension):
        from pymilvus import FieldSchema, DataType, CollectionSchema, Collection

        schema = [
            FieldSchema(
                "pk", DataType.VARCHAR, is_primary=True, auto_id=False, max_length=64000
            ),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=dimension),
            FieldSchema("sample_id", DataType.VARCHAR, max_length=64000),
        ]
        col_schema = CollectionSchema(schema)
        col = Collection(
            self.config.collection_name,
            col_schema,
            consistency_level=self.config.consistency_level,
            using=self.alias,
        )
        col.create_index("vector", index_params=self.config.index_params)
        col.load()
        return col

    def get_collection(self):
        from pymilvus import Collection

        return Collection(self.config.collection_name, using=self.alias)

    def _get_existing_ids(self, ids):
        ids = ['"' + str(entry) + '"' for entry in ids]
        expr = f"""pk in [{','.join(ids)}]"""
        ids = self.get_collection().query(expr)
        return ids

    def _delete_ids(self, ids):
        ids = ['"' + str(entry) + '"' for entry in ids]
        expr = f"""pk in [{','.join(ids)}]"""
        self.get_collection().delete(expr)

    def _get_embeddings(self, ids):
        from pymilvus import utility
        ids = ['"' + str(entry) + '"' for entry in ids]
        expr = f"""pk in [{','.join(ids)}]"""
        # logger.error("get embedding:" + self.config.collection_name)
        # with open("/Users/filiphaltmayer/Documents/packages_clean/voxel51/broken.txt", "w+" ) as f:
            # f.write("get embedding:" + self.config.collection_name)
            # f.write(" ".join(utility.list_collections(using=self.alias)))
        logger.error("get embedding:" + self.config.collection_name)
        data = self.get_collection().query(
            expr, output_fields=["pk", "sample_id", "vector"]
        )
        return data

    def remove_from_index(
        self,
        sample_ids=None,
        label_ids=None,
        allow_missing=True,
        warn_missing=False,
    ):
        if label_ids is not None:
            ids = label_ids
        else:
            ids = sample_ids

        if not allow_missing or warn_missing:
            existing_ids = self.get_existing_ids(ids)
            missing_ids = set(existing_ids) - set(ids)
            num_missing = len(missing_ids)

            if num_missing > 0:
                if not allow_missing:
                    raise ValueError(
                        "Found %d IDs (eg %s) that are not present in the "
                        "index" % (num_missing, missing_ids[0])
                    )

                if warn_missing:
                    logger.warning(
                        "Ignoring %d IDs that are not present in the index",
                        num_missing,
                    )

        self._delete_ids(ids=ids)

    def get_embeddings(
        self,
        sample_ids=None,
        label_ids=None,
        allow_missing=True,
        warn_missing=False,
    ):
        if label_ids is not None:
            if self.config.patches_field is None:
                raise ValueError("This index does not support label IDs")

            if sample_ids is not None:
                logger.warning("Ignoring sample IDs when label IDs are provided")

        if sample_ids is not None and self.config.patches_field is not None:
            (
                embeddings,
                sample_ids,
                label_ids,
                missing_ids,
            ) = self._get_patch_embeddings_from_sample_ids(sample_ids)
        elif self.config.patches_field is not None:
            (
                embeddings,
                sample_ids,
                label_ids,
                missing_ids,
            ) = self._get_patch_embeddings_from_label_ids(label_ids)
        else:
            (
                embeddings,
                sample_ids,
                label_ids,
                missing_ids,
            ) = self._get_sample_embeddings(sample_ids)

        num_missing_ids = len(missing_ids)
        if num_missing_ids > 0:
            if not allow_missing:
                raise ValueError(
                    "Found %d IDs (eg %s) that do not exist in the index"
                    % (num_missing_ids, missing_ids[0])
                )

            if warn_missing:
                logger.warning(
                    "Skipping %d IDs that do not exist in the index",
                    num_missing_ids,
                )

        embeddings = np.array(embeddings)
        sample_ids = np.array(sample_ids)
        if label_ids is not None:
            label_ids = np.array(label_ids)

        return embeddings, sample_ids, label_ids

    def cleanup(self):
        self.get_collection().drop()

    def _get_sample_embeddings(self, sample_ids, batch_size=1000):
        found_embeddings = []
        found_sample_ids = []

        if sample_ids is None:
            raise ValueError(
                "Milvus does not support retrieving all vectors in an index"
            )

        for batch_ids in fou.iter_batches(sample_ids, batch_size):
            response = self._get_embeddings(list(batch_ids))

            for r in response:
                found_embeddings.append(r["vector"])
                found_sample_ids.append(r["sample_id"])

        missing_ids = list(set(sample_ids) - set(found_sample_ids))

        return found_embeddings, found_sample_ids, None, missing_ids

    def _get_patch_embeddings_from_label_ids(self, label_ids, batch_size=1000):
        found_embeddings = []
        found_sample_ids = []
        found_label_ids = []

        if label_ids is None:
            raise ValueError(
                "Milvus does not support retrieving all vectors in an index"
            )

        for batch_ids in fou.iter_batches(label_ids, batch_size):
            response = self.get_embeddings(list(batch_ids))

            for r in response:
                found_embeddings.append(r["vector"])
                found_sample_ids.append(r["sample_id"])
                found_label_ids.append(r["pk"])

        missing_ids = list(set(label_ids) - set(found_label_ids))

        return found_embeddings, found_sample_ids, found_label_ids, missing_ids

    def _get_patch_embeddings_from_sample_ids(self, sample_ids, batch_size=100):
        found_embeddings = []
        found_sample_ids = []
        found_label_ids = []

        query_vector = [0.0] * self.get_dim()
        top_k = min(batch_size, self.config.max_k)

        for batch_ids in fou.iter_batches(sample_ids, batch_size):
            ids = ['"' + str(entry) + '"' for entry in batch_ids]
            expr = f"""pk in [{','.join(ids)}]"""
            response = self.get_collection().search(
                data=[query_vector],
                anns_field="vector",
                param=self.config.search_params,
                expr=expr,
                limit=top_k,
            )
            ids = [x.id for x in response[0]]
            response = self._get_embeddings(ids)
            for r in response:
                found_embeddings.append(r["vector"])
                found_sample_ids.append(r["sample_id"])
                found_label_ids.append(r["pk"])

        missing_ids = list(set(sample_ids) - set(found_sample_ids))

        return found_embeddings, found_sample_ids, found_label_ids, missing_ids

    def _kneighbors(
        self,
        query=None,
        k=None,
        reverse=False,
        aggregation=None,
        return_dists=False,
    ):
        if query is None:
            raise ValueError("Milvus does not support full index neighbors")

        if reverse is True:
            raise ValueError("Milvus does not support least similarity queries")

        if k is None or k > self.config.max_k:
            raise ValueError("Milvus requires k<=%s" % self.config.max_k)

        if aggregation not in (None, "mean"):
            raise ValueError("Unsupported aggregation '%s'" % aggregation)

        query = self._parse_neighbors_query(query)
        if aggregation == "mean" and query.ndim == 2:
            query = query.mean(axis=0)

        single_query = query.ndim == 1
        if single_query:
            query = [query]

        if self.config.patches_field is not None:
            index_ids = self.current_label_ids
        else:
            index_ids = self.current_sample_ids

        expr = ['"' + str(entry) + '"' for entry in index_ids]
        expr = f"""pk in [{','.join(expr)}]"""

        ids = []
        dists = []
        for q in query:
            response = self.get_collection().search(
                data=[q.tolist()],
                anns_field="vector",
                limit=k,
                expr=expr,
                param=self.config.search_params,
            )
            ids.append([r.id for r in response[0]])
            if return_dists:
                dists.append([r.score for r in response[0]])

        if single_query:
            ids = ids[0]
            if return_dists:
                dists = dists[0]

        if return_dists:
            return ids, dists

        return ids

    def _parse_neighbors_query(self, query):
        if etau.is_str(query):
            query_ids = [query]
            single_query = True
        else:
            query = np.asarray(query)

            # Query by vector(s)
            if np.issubdtype(query.dtype, np.number):
                return query

            query_ids = list(query)
            single_query = False

        # Query by ID(s)
        response = self._get_embeddings(query_ids)
        query = np.array([x["vector"] for x in response])

        if single_query:
            query = query[0, :]

        return query

    @classmethod
    def _from_dict(cls, d, samples, config, brain_key):
        return cls(samples, config, brain_key)
