FROM ankurgos/cosmos-base:1.3

COPY cosmos/ingestion /ingestion
WORKDIR /ingestion
RUN pip install .

COPY deployment/weights/model_weights.pth /weights/model_weights.pth
COPY deployment/weights/pp_model_weights.pth /weights/pp_model_weights.pth
COPY deployment/configs /configs
COPY cli/ingest_documents.sh /cli/ingest_documents.sh
RUN chmod +x /cli/ingest_documents.sh

CMD /cli/ingest_documents.sh
