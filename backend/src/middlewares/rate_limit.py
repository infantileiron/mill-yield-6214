from starlette.middleware.base import BaseHTTPMiddleware
import logging,time
from redis import Redis
from src.config.settings import REDIS_URL
class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        # Rate limiting is abuse mitigation only. If Redis is down, fail open so
        # accepted reviews can still be read and submitted; the worker's durable
        # recovery scan handles the queue outage separately.
        if request.url.path!='/health':
            try:
                redis=Redis.from_url(REDIS_URL,socket_connect_timeout=1,socket_timeout=2);key=f'mill:limit:{request.client.host}:{int(time.time()//60)}'
                n=redis.incr(key)
                if n==1:redis.expire(key,65)
                if n>240:return _too_many()
            except Exception:
                logging.warning('rate limiter unavailable, allowing request')
        return await call_next(request)
def _too_many():
    from starlette.responses import JSONResponse
    return JSONResponse({'detail':'请求频率过高，请一分钟后重试'},status_code=429)
