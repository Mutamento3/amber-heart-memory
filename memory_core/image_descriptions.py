"""Safe final image-description results; never original image data or URLs."""

import math
import re
from .extraction_contract import check_credentials

MAX_DESCRIPTION_CHARS = 2000


def valid_usage(value):
    if value is None:
        return True
    return isinstance(value,dict) and all(isinstance(k,str) and re.fullmatch(r'[a-z_]{1,64}',k)
        and (valid_usage(v) if v is None or isinstance(v,dict) else type(v) in (int,float) and math.isfinite(v) and v>=0)
        for k,v in value.items())


def validate_results(results, image_count):
    if not isinstance(results,list) or len(results)!=image_count or not image_count:
        raise ValueError('图片描述与原消息的图片数量不符')
    for result in results:
        if not isinstance(result,dict) or set(result)!={'description','calls'}:
            raise ValueError('图片描述结果格式无效')
        description,calls=result['description'],result['calls']
        if description is not None:
            if (not isinstance(description,str) or not description.strip() or len(description)>MAX_DESCRIPTION_CHARS
                    or '\x00' in description or 'data:image' in description.lower()):
                raise ValueError('图片描述正文格式无效')
            check_credentials(description)
        if not isinstance(calls,list) or not 1<=len(calls)<=2:
            raise ValueError('图片描述调用记录无效')
        for call in calls:
            if not isinstance(call,dict) or set(call)!={'model','state','usage','code'}:
                raise ValueError('图片描述调用记录无效')
            if not isinstance(call['model'],str) or not 1<=len(call['model'])<=256:
                raise ValueError('图片描述型号无效')
            check_credentials(call['model'])
            if call['state'] not in ('succeeded','failed','blocked') or not valid_usage(call['usage']):
                raise ValueError('图片描述调用状态或用量无效')
            if call['code'] is not None and (not isinstance(call['code'],str)
                    or not re.fullmatch(r'image_description_[a-z0-9_]{1,80}',call['code'])):
                raise ValueError('图片描述错误码无效')
        if (description is not None)!=(calls[-1]['state']=='succeeded'):
            raise ValueError('图片描述正文与调用状态不符')
    return results
