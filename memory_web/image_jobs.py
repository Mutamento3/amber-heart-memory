"""Bounded in-memory work; raw images are never queued in the database."""

import asyncio
import logging
from starlette.concurrency import run_in_threadpool
from memory_core.chat_text import IMAGE_DESCRIPTION_PROMPT
from memory_core.message_images import MessageImages
from .image_model import ImageModel

LOG=logging.getLogger('memory_web.images')


class ImageJobs:
    def __init__(self,settings,models=None,*,on_saved=None):
        self.settings,self.models=settings,models or ImageModel(settings)
        self.on_saved=on_saved
        self.tasks={}
        self.slots=asyncio.Semaphore(settings.image_description_concurrency)
        self.stopping=False

    def schedule(self,space,record,message):
        if self.stopping or not self.models.configured or record['step_index']!=0 or message['role']!='user':
            return False
        parts=message.get('content')
        images=[{k:v for k,v in p['image_url'].items() if k in ('url','detail')}
            for p in parts if p.get('type')=='image_url'] if isinstance(parts,list) else []
        if not images:
            return False
        if len(images)>self.settings.image_description_max_images:
            LOG.warning('image_description_skipped space=%s turn_id=%s code=image_limit',space,record['turn_id'])
            return False
        key=(space,record['turn_id'])
        if key in self.tasks:
            return False
        if len(self.tasks)>=self.settings.image_description_max_pending:
            LOG.warning('image_description_skipped space=%s turn_id=%s code=capacity',space,record['turn_id'])
            return False
        task=asyncio.create_task(self.run(space,record,images))
        self.tasks[key]=task
        task.add_done_callback(lambda done:self.tasks.pop(key,None))
        return True

    async def run(self,space,record,images):
        try:
            store=await run_in_threadpool(MessageImages,self.settings.dsn,space)
            source=await run_in_threadpool(store.source,record)
            if source is None:
                return
            if source['image_count']!=len(images):
                LOG.warning('image_description_skipped space=%s turn_id=%s code=image_count_mismatch',space,record['turn_id'])
                return
            names=source['participants']
            prompt=IMAGE_DESCRIPTION_PROMPT.format(user_name=names['user']['name'],assistant_name=names['assistant']['name'])
            results=[None]*len(images)
            iterator=iter(enumerate(images))
            async def worker():
                for index,image in iterator:
                    async with self.slots:
                        results[index]=await self.models.describe(image,prompt)
            async with asyncio.TaskGroup() as group:
                for _ in range(min(len(images),self.settings.image_description_concurrency)):
                    group.create_task(worker())
            saved=await run_in_threadpool(store.publish,source['message_id'],results)
            if saved and self.on_saved and any(r['description'] for r in results):
                self.on_saved(space,record['turn_id'])
            if saved:
                for index,result in enumerate(results):
                    if result['description'] is None:
                        LOG.warning('image_description_failed space=%s message_id=%s image=%s code=%s',
                            space,source['message_id'],index,result['calls'][-1]['code'])
        except Exception as exc:
            LOG.warning('image_description_job_failed space=%s turn_id=%s type=%s code=%s',
                space,record['turn_id'],type(exc).__name__,getattr(exc,'sqlstate',None) or 'image_description_job_failed')

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))

    async def stop(self):
        self.stopping=True
        tasks=list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
