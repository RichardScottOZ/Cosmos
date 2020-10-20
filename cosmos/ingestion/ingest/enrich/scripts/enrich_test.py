from enrich import Enrich
import logging

logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)

filepath = '/hdd/iain/covid_output/'
dataset_id = 'covid_docs_all'


def enrich_ingest_output(filepath, dataset_id):
    enrich = Enrich(filepath, dataset_id)
    enrich.semantic_enrichment()


enrich_ingest_output(filepath=filepath,
                     dataset_id=dataset_id)
