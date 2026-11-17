"""Vision input via chat completions; only explicit filtering uses the backup."""

from memory_core.http_transport import post_json,ModelCallError,is_content_filter
from memory_core.image_descriptions import validate_results
from memory_core.model_config import ModelRole
from .extraction_model import safe_usage


class ImageModel:
    def __init__(self,settings,*,transport=None):
        self.role=ModelRole(settings.image_description_model,settings.image_description_base_url,
            settings.image_description_api_key).resolve(settings.upstream_base_url,settings.upstream_api_key)
        self.models=list(dict.fromkeys(m for m in (self.role.model,settings.image_description_backup_model) if m))
        self.settings,self.transport=settings,transport

    @property
    def configured(self):
        return self.role.configured

    async def describe(self,image,prompt):
        calls=[]
        for model in self.models:
            value=None
            try:
                value=await post_json(self.role.base_url,self.role.api_key,'chat/completions',dict(
                    model=model,stream=False,max_tokens=800,messages=[dict(role='user',content=[
                        dict(type='image_url',image_url=image),dict(type='text',text=prompt)])]),
                    role='image_description',timeout=self.settings.image_description_timeout_seconds,
                    transport=self.transport,classify_content_filter=True)
                choices=value.get('choices')
                if not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict):
                    raise ModelCallError('image_description_invalid_response')
                choice=choices[0];message=choice.get('message') or {}
                content=message.get('content')
                refused=(isinstance(message.get('refusal'),str) and bool(message['refusal'].strip())) or (
                    isinstance(content,list) and any(isinstance(p,dict) and p.get('type')=='refusal' for p in content))
                if choice.get('finish_reason')=='content_filter' or refused or is_content_filter(choice.get('error')):
                    raise ModelCallError('image_description_content_filter')
                if choice.get('finish_reason')!='stop' or not isinstance(content,str) or not content.strip():
                    raise ModelCallError('image_description_invalid_response')
                description=content.strip()
                if any(key and key in description for key in (self.role.api_key,self.settings.upstream_api_key)):
                    raise ModelCallError('image_description_invalid_response')
                call=dict(model=model,state='succeeded',usage=safe_usage(value.get('usage')),code=None)
                result=dict(description=description,calls=calls+[call])
                validate_results([result],1)
                return result
            except Exception as exc:
                blocked=isinstance(exc,ModelCallError) and exc.code=='image_description_content_filter'
                code=exc.code if isinstance(exc,ModelCallError) and exc.code.startswith('image_description_') else 'image_description_invalid_response'
                calls.append(dict(model=model,state='blocked' if blocked else 'failed',code=code,
                    usage=safe_usage(value.get('usage') if isinstance(value,dict) else getattr(exc,'usage',None))))
                if not blocked:
                    break
        return dict(description=None,calls=calls)
