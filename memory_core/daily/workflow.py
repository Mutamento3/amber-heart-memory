"""Synchronous local workflow. Extraction requires an explicit fixed-response file."""

import argparse
from datetime import datetime
import json
from pathlib import Path

import psycopg

from ..cards.drafts import DraftStore
from ..testing.fixed_models import FixedModels


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--space', required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    submit = actions.add_parser('submit')
    submit.add_argument('file', type=Path)
    extract = actions.add_parser('extract')
    extract.add_argument('id', type=int)
    extract.add_argument('--fixed-responses', type=Path, required=True)
    for name in ('show', 'extraction', 'source'):
        actions.add_parser(name).add_argument('id', type=int)
    listing = actions.add_parser('list')
    listing.add_argument('--after-id', type=int, default=0)
    listing.add_argument('--limit', type=int, default=50)
    edit = actions.add_parser('edit')
    edit.add_argument('id', type=int)
    edit.add_argument('file', type=Path)
    edit.add_argument('--version', type=int, required=True)
    edit.add_argument('--revision', type=int, required=True)
    reject = actions.add_parser('reject')
    reject.add_argument('id', type=int)
    reject.add_argument('--version', type=int, required=True)
    actions.add_parser('delete-source').add_argument('id', type=int)
    actions.add_parser('expire').add_argument('--now', required=True)
    reconfirm = actions.add_parser('reconfirm')
    reconfirm.add_argument('id', type=int)
    reconfirm.add_argument('--version', type=int, required=True)
    reconfirm.add_argument('--revision', type=int, required=True)
    approve = actions.add_parser('approve')
    approve.add_argument('id', type=int)
    approve.add_argument('--version', type=int, required=True)
    approve.add_argument('--revision', type=int, required=True)
    approve.add_argument('--request-key', required=True)
    approve.add_argument('--embedding-label', required=True)
    approve.add_argument('--fixed-responses', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        store = DraftStore.from_env(args.space)
        if args.action=='submit':
            value = json.loads(args.file.read_text(encoding='utf-8'))
            result = {'extraction_id':store.submit(**value)}
        elif args.action=='extract':
            models = FixedModels.from_file(args.fixed_responses)
            value = store.extract(args.id,selector=models,structurer=models)
            result = {'extraction_id':value['id'],'status':value['status'],
                      'draft_ids':[d['id'] for d in value['drafts']], 'error_code':value['error_code']}
        elif args.action=='show':
            result = store.get(args.id)
        elif args.action=='extraction':
            result = store.extraction(args.id)
        elif args.action=='source':
            result = store.source(args.id)
        elif args.action=='list':
            result = store.list(after_id=args.after_id,limit=args.limit)
        elif args.action=='edit':
            result = store.edit(args.id,json.loads(args.file.read_text(encoding='utf-8')),
                                expected_version=args.version,expected_policy_revision=args.revision)
        elif args.action=='reject':
            result = store.reject(args.id,expected_version=args.version)
        elif args.action=='delete-source':
            store.delete_material(args.id)
            result = {'deleted':True}
        elif args.action=='reconfirm':
            result = store.reconfirm_extraction(args.id,expected_version=args.version,expected_policy_revision=args.revision)
        elif args.action=='approve':
            from ..cards.review import ReviewStore
            store = ReviewStore.from_env(args.space)
            models = FixedModels.from_file(args.fixed_responses)
            result = store.approve(args.id,expected_version=args.version,expected_policy_revision=args.revision,
                                   request_key=args.request_key,embedder=models,embedding_model=args.embedding_label)
        else:
            result = {'expired':store.expire_materials(now=datetime.fromisoformat(args.now))}
    except psycopg.Error as exc:
        parser.exit(2,f'操作停止：数据库校验失败（SQLSTATE {exc.sqlstate or "unknown"}）\n')
    except (ValueError, TypeError, OSError) as exc:
        # JSON decoder errors contain snippets, and arbitrary caller types can
        # include material values. Emit only the type at the CLI boundary.
        parser.exit(2,f'操作停止：输入、版本或固定响应无效（{type(exc).__name__}）\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__=='__main__':
    main()
