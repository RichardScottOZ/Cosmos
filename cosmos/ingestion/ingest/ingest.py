import pickle
import shutil
import functools
import json
import os
from PIL import Image
import io
import subprocess
import glob
from ingest.process_page import propose_and_pad, xgboost_postprocess, rules_postprocess
from ingest.detect import detect
from dask.distributed import Client, progress
import dask.dataframe as dd
from ingest.utils.preprocess import resize_png
from ingest.utils.pdf_helpers import get_pdf_names
from ingest.utils.pdf_extractor import parse_pdf
from ingest.process.ocr.ocr import regroup, pool_text
from ingest.process.aggregation.aggregate import aggregate_router
from tqdm import tqdm
import pikepdf
import pandas as pd

import logging
logging.basicConfig(format='%(levelname)s :: %(filename) :: %(funcName)s :: %(asctime)s :: %(message)s', level=logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("PIL").setLevel(logging.ERROR)
logging.getLogger("ingest.detect").setLevel(logging.ERROR)
logging.getLogger("ingest.process.detection.src.torch_model.model.model").setLevel(logging.ERROR)
logging.getLogger("ingest.process.detection.src.utils.ingest_images").setLevel(logging.ERROR)
logging.getLogger("ingest.process.detection.src.torch_model.train.data_layer.xml_loader").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


class Ingest:
    def __init__(self, scheduler_address, use_semantic_detection=False, client=None,
                       tmp_dir=None, use_xgboost_postprocess=False, use_rules_postprocess=False):
        logger.info("Initializing Ingest object")
        self.client = client
        if self.client is None:
            logger.info("Setting up client")
            self.client = Client(scheduler_address, serializers=['msgpack', 'dask'], deserializers=['msgpack', 'dask', 'pickle'])
            logger.info(self.client)
        self.use_xgboost_postprocess = use_xgboost_postprocess
        self.use_rules_postprocess = use_rules_postprocess
        self.use_semantic_detection = use_semantic_detection
        self.tmp_dir = tmp_dir
        if self.tmp_dir is not None:
            # Create a subdirectory for tmp files
            self.images_tmp = os.path.join(self.tmp_dir, 'images')
            os.makedirs(self.images_tmp, exist_ok=True)

    def __del__(self):
        if self.client is not None:
            self.client.close()

    def ingest(self, pdf_directory, dataset_id, result_path, skip_ocr=True, visualize_proposals=False, aggregations=[]):
        if self.tmp_dir is not None:
            self._ingest_local(pdf_directory,
                               dataset_id,
                               result_path,
                               visualize_proposals=visualize_proposals,
                               skip_ocr=skip_ocr,
                               aggregations=aggregations)
        else:
            self._ingest_distributed(pdf_directory, dataset_id)

    def _ingest_local(self, pdf_directory, dataset_id, result_path, visualize_proposals=False, aggregate=False, skip_ocr=True, aggregations=[]):
        pdfnames = get_pdf_names(pdf_directory)
        pdf_to_images = functools.partial(Ingest.pdf_to_images, dataset_id, self.images_tmp)
        logger.info('Starting ingestion. Converting PDFs to images.')
        images = [self.client.submit(pdf_to_images, pdf, resources={'process': 1}) for pdf in pdfnames]
        progress(images)
        logger.info('Done converting to images. Starting detection and text extraction')
        images = [i.result() for i in images]
        images = [i for i in images if i is not None]
        images = [i for il in images for i in il]

        partial_propose = functools.partial(propose_and_pad, visualize=visualize_proposals)
        images = self.client.map(partial_propose, images, resources={'process': 1}, priority=8)
        if self.use_semantic_detection:
            images = self.client.map(detect, images, resources={'GPU': 1}, priority=8)
            images = self.client.map(regroup, images, resources={'process': 1})
            pool_text_ocr_opt = functools.partial(pool_text, skip_ocr=skip_ocr)
            images = self.client.map(pool_text_ocr_opt, images, resources={'process': 1})
            if self.use_xgboost_postprocess:
                images = self.client.map(xgboost_postprocess, images, resources={'process': 1})
                if self.use_rules_postprocess:
                    images = self.client.map(rules_postprocess, images, resources={'process': 1})
        progress(images)
        images = [i.result() for i in images]
        results = []
        for i in images:
            with open(i, 'rb') as rf:
                obj = pickle.load(rf)
                for ind, c in enumerate(obj['content']):
                    bb, cls, text = c
                    scores, classes = zip(*cls)
                    scores = list(scores)
                    classes = list(classes)
                    postprocess_cls = postprocess_score = None
                    if 'xgboost_content' in obj:
                        _, postprocess_cls, _, postprocess_score = obj['xgboost_content'][ind]
                    final_obj = {'pdf_name': obj['pdf_name'], 
                                 'dataset_id': obj['dataset_id'],
                                 'page_num': obj['page_num'], 
                                 'bounding_box': list(bb),
                                 'classes': classes,
                                 'scores': scores,
                                 'content': text,
                                 'postprocess_cls': postprocess_cls,
                                 'postprocess_score': postprocess_score
                                }
                    results.append(final_obj)
        result_df = pd.DataFrame(results)
        result_df['detect_cls'] = result_df['classes'].apply(lambda x: x[0])
        result_df['detect_score'] = result_df['scores'].apply(lambda x: x[0])
        for aggregation in aggregations:
            aggregate_df = aggregate_router(result_df, aggregate_type=aggregation)
            name = f'{dataset_id}_{aggregation}.parquet'
            aggregate_df.to_parquet(os.path.join(result_path, name), engine='pyarrow', compression='gzip')
        result_df.to_parquet(os.path.join(result_path, f'{dataset_id}.parquet'), engine='pyarrow', compression='gzip')
        shutil.rmtree(self.tmp_dir)

    def _ingest_distributed(self, pdf_directory, dataset_id):
        raise NotImplementedError("Distributed setup currently not implemented via Ingest class. Set a tmp directory.")

    def write_images_for_annotation(self, pdf_dir, img_dir):
        logger.info(f"Converting PDFs to images and writing to target directory: {img_dir}")
        pdfnames = get_pdf_names(pdf_dir)
        pdf_to_images = functools.partial(Ingest.pdf_to_images, 'na', self.images_tmp)
        images = [self.client.submit(pdf_to_images, pdf, resources={'process': 1}) for pdf in pdfnames]
        progress(images)
        images = [i.result() for i in images]
        images = [i for i in images if i is not None]
        images = [i for il in images for i in il]
        paths = [f'{tmp_dir}/{pdf_name}_{pn}' for tmp_dir, pdf_name, pn in images]
        for path in paths:
            bname = os.path.basename(path)
            new_bname = bname + '.png'
            shutil.copy(path, os.path.join(img_dir, new_bname))
        logger.info('Done.')
        shutil.rmtree(self.tmp_dir)

    @classmethod
    def remove_watermarks(cls, pdf_directory, target_directory):
        logger.info(f'Removing watermarks. Moving to {target_directory}')
        remove_w_target = functools.partial(Ingest._remove_watermark, target_directory=target_directory)
        pdfs = glob.glob(os.path.join(pdf_directory, "*"))
        for p in tqdm(pdfs):
            remove_w_target(p)
            # TODO: client.submit wasn't writing to target correctly, fix later
        logger.info('Done')

    @classmethod
    def _remove_watermark(cls, filename, target_directory):
        try:
            target = pikepdf.Pdf.open(filename)
            new = pikepdf.Pdf.new()
            for ind, page in enumerate(target.pages):
                commands = []
                BDC = False
                for operands, operator in pikepdf.parse_content_stream(page):
                    if str(operator) == 'BDC':
                        BDC = True
                        continue
                    if BDC:
                        if str(operator) == 'EMC':
                            BDC = False
                            continue
                        continue
                    commands.append((operands, operator))
                new_content_stream = pikepdf.unparse_content_stream(commands)
                new.add_blank_page()
                new.pages[ind].Contents = new.make_stream(new_content_stream)
                new.pages[ind].Resources = new.copy_foreign(target.make_indirect(target.pages[ind].Resources))
            new.remove_unreferenced_resources()
            new.save(os.path.join(target_directory, os.path.basename(filename)))
            return filename
        except pikepdf._qpdf.PdfError as e:
            logger.error(f'Error in file: {filename}')
            logger.error(e)
            return
        except RuntimeError as e:
            logger.error(f'Error in file: {filename}')
            logger.error(e)
            return

    @classmethod
    def pdf_to_images(cls, dataset_id, tmp_dir, filename):
        if filename is None:
            return None
        pdf_name = os.path.basename(filename)
        limit = None
        try:
            meta, limit = parse_pdf(filename)
            logger.debug(f'Limit: {limit}')
        except TypeError as te:
            logger.error(str(te), exc_info=True)
            logger.error(f'Logging TypeError for pdf: {pdf_name}')
            return []
        except Exception as e:
            logger.error(str(e), exc_info=True)
            logger.error(f'Logging parsing error for pdf: {pdf_name}')
            return []
        subprocess.run(['gs', '-dBATCH',
                        '-dNOPAUSE',
                        '-sDEVICE=png16m',
                        '-dGraphicsAlphaBits=4',
                        '-dTextAlphaBits=4',
                        '-r600',
                        f'-sOutputFile="{tmp_dir}/{pdf_name}_%d"',
                        filename
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        objs = []
        names = glob.glob(f'{tmp_dir}/{pdf_name}_?')
        for image in names:
            try:
                page_num = int(image[-1])
            except ValueError:
                raise Exception(f'{image}')
            with open(image, 'rb') as bimage:
                bstring = bimage.read()
            bytesio = io.BytesIO(bstring)
            img = Image.open(bytesio).convert('RGB')
            orig_w, orig_h = img.size
            meta2 = None
            img, img_size = resize_png(img, return_size=True)
            w, h = img_size
            dims = [0, 0, w, h]
            if meta is not None:
                orig_w = limit[2]
                orig_h = limit[3]
                scale_w = w / orig_w
                scale_h = h / orig_h
                logger.debug(f'Original w: {orig_w}')
                logger.debug(f'Original h: {orig_h}')
                logger.debug(f'New w: {w}')
                logger.debug(f'New h: {h}')
                meta2 = meta.copy()
                meta2.loc[meta2.page == (page_num-1), 'x1'] = meta2.x1 * scale_w
                meta2.loc[meta2.page == (page_num-1), 'x2'] = meta2.x2 * scale_w
                meta2.loc[meta2.page == (page_num-1), 'y1'] = meta2.y1 * scale_h
                meta2.loc[meta2.page == (page_num-1), 'y2'] = meta2.y2 * scale_h
                meta2 = meta2.to_dict()
                meta2 = json.dumps(meta2)
                meta2 = json.loads(meta2)

            # Convert it back to bytes
            img.save(image, format='PNG')
            obj = {'orig_w': orig_w, 'orig_h': orig_h, 'dataset_id': dataset_id, 'pdf_name': pdf_name, 'meta': meta2, 'dims': dims, 'pdf_limit': limit, 'page_num': page_num}
            if tmp_dir is not None:
                with open(os.path.join(tmp_dir, pdf_name) + f'_{page_num}.pkl', 'wb') as wf:
                    pickle.dump(obj, wf)
                objs.append((tmp_dir, pdf_name, page_num))
            else:
                objs.append(obj)
        return objs

