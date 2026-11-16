"""跨市场设备服务质量分析：sampling模块。"""
import hashlib
from .errors import require
from .lineage import Lineage
from .publication import Publication
from .store import Store


class Sampling:
    def __init__(self, store: Store):
        self.store = store
        self.publication = Publication(store)
        self.lineage = Lineage(store)

    def select(self, market: str, version: int, size: int, seed: str) -> dict:
        require(type(size) is int and 1 <= size <= 100, "invalid_size", "sample size must be 1..100")
        require(isinstance(seed, str) and 1 <= len(seed) <= 100, "invalid_seed", "seed required")
        release = self.publication.read(market, version)
        scored = []
        for row in release["rows"]:
            key = f"{market}|{version}|{seed}|{row['day']}|{row['product']}"
            score = hashlib.sha256(key.encode()).hexdigest()
            scored.append((score, row))
        scored.sort(key=lambda item: item[0])
        selected = []
        for score, row in scored[:size]:
            trace = self.lineage.explain(market, version, row["day"], row["product"])
            selected.append({"score": score, "day": row["day"], "product": row["product"], "metric": row, "source_event_count": len(trace["source_rows"]), "device_count": len(trace["devices"])})
        return {"market": market, "version": version, "seed": seed, "requested": size, "available": len(scored), "samples": selected}
