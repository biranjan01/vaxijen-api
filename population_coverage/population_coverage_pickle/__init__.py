import pickle
import os

_pickle_path = os.path.join(os.path.dirname(__file__), "population_genotype_map.p")

with open(_pickle_path, "rb") as _f:
    population_coverage = pickle.load(_f)
    country_ethnicity = pickle.load(_f)
    ethnicity = pickle.load(_f)
