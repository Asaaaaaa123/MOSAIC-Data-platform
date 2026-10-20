"""Cross-database linking: identifier clustering, federated search and entity resolution."""

from .merge import LINKING_KEYS, cluster_records, merge_cluster

__all__ = ["LINKING_KEYS", "cluster_records", "merge_cluster"]
