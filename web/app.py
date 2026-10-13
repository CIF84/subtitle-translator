"""Local HTTP boundary. No subprocess CLI calls, CORS, keys, or filesystem APIs."""
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import os
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .service import Jobs, WebError, MAX_UPLOAD
import translate_srt as tr

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: StrictStr = Field(default='en',max_length=32)
    target: StrictStr = Field(default='cs',max_length=32)
    translation_model: StrictStr = 'gpt-5.4-mini'
    revision_enabled: StrictBool = False
    revision_model: StrictStr = 'gpt-5.4'
    max_cost: StrictStr = '1.00'


class Approval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: StrictBool = False
    quote_id: StrictStr = Field(default='', max_length=64)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['accept','reject','edit']
    text: StrictStr | None = Field(default=None,max_length=100000)
    expected_version: StrictStr | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')


class ReviewConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: StrictBool = False
    expected_version: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    operation_id: StrictStr = Field(pattern=r'^[a-f0-9]{32}$')


class BulkDecision(ReviewConfirmation):
    action: Literal['accept','reject']


class FullExport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: StrictStr | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        count = 0
        async def limited_receive():
            nonlocal count
            message = await receive()
            count += len(message.get('body',b''))
            if count > MAX_UPLOAD:
                raise HTTPException(413,'Request exceeds the size limit.')
            return message
        await self.app(scope,limited_receive if scope['type']=='http' else receive,send)


def create_app(root=ROOT, client_factory=None, executor=None):
    service = Jobs(root, client_factory, executor)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs = service
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost'])

    @app.middleware('http')
    async def local_only(request, call_next):
        origin = request.headers.get('origin')
        if origin and origin not in ('http://127.0.0.1:8000','http://localhost:8000'):
            return JSONResponse({'error':'Requests must originate from the local application.'},403)
        if request.headers.get('sec-fetch-site') not in (None,'none','same-origin'):
            return JSONResponse({'error':'Cross-site requests are blocked.'},403)
        if request.method not in ('GET','HEAD'):
            if not (secrets.compare_digest(request.headers.get('x-csrf-token',''),token)
                    and secrets.compare_digest(request.cookies.get('subtitle_session',''),token)):
                return JSONResponse({'error':'Refresh the local application before submitting.'},403)
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length)>MAX_UPLOAD):
            return JSONResponse({'error':'Request exceeds the size limit.'},413)
        response = await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(WebError)
    async def web_error(_, error):
        return JSONResponse({'error':str(error)}, error.status)

    @app.exception_handler(tr.TranslationError)
    async def domain_error(_, error):
        return JSONResponse({'error':str(error)},400)

    @app.exception_handler(OSError)
    async def filesystem_error(_, error):
        return JSONResponse({'error':'Local file operation failed; existing files and spending records were retained.'},409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, error):
        return JSONResponse({'error':'Invalid request fields or types.'},422)

    @app.get('/api/health')
    def health():
        return {'application':'subtitle-translator-wp3','instance':getattr(app.state,'instance',None),'pid':os.getpid()}

    @app.get('/api/session')
    def session():
        response = JSONResponse({'csrf_token':token})
        response.set_cookie('subtitle_session', token, httponly=True, samesite='strict')
        return response

    @app.get('/')
    def page():
        return FileResponse(Path(__file__).parent/'static/index.html')

    app.mount('/static', StaticFiles(directory=Path(__file__).parent/'static'), name='static')

    @app.get('/api/options')
    def options():
        return {'languages':tr.LANGUAGES,'models':[m for m in tr.VERIFIED_REASONING], 'upload_limit':MAX_UPLOAD}

    @app.get('/api/jobs')
    def jobs():
        return service.listing()

    @app.post('/api/jobs',status_code=201)
    async def upload(request:Request, filename:str):
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>MAX_UPLOAD:
                raise WebError('File exceeds the 5 MiB upload limit.',413)
            chunks.append(chunk)
        return await run_in_threadpool(service.upload,filename,b''.join(chunks))

    @app.get('/api/jobs/{ident}')
    def job(ident:str):
        return service.public(ident)

    @app.get('/api/jobs/{ident}/history/{operation}')
    def comparable_history(ident:str, operation:Literal['translation','revision']):
        from .presentation import history
        return history(service,ident,operation)

    @app.get('/api/jobs/{ident}/revision-cost/{quote_id}')
    def revision_cost(ident:str, quote_id:str):
        from .calibration import view
        return view(service,ident,quote_id)

    @app.put('/api/jobs/{ident}/config')
    def configure(ident:str, body:Settings):
        return service.configure(ident,body.model_dump())

    @app.post('/api/jobs/{ident}/estimate/{operation}')
    def estimate(ident:str, operation:Literal['translation','revision']):
        return service.estimate(ident,operation)

    @app.post('/api/jobs/{ident}/approve/{operation}',status_code=202)
    def approve(ident:str, operation:Literal['translation','revision'], body:Approval):
        return service.start(ident,operation,body.approved,body.quote_id)

    @app.post('/api/jobs/{ident}/pause')
    def pause(ident:str):
        return service.pause(ident)

    @app.get('/api/jobs/{ident}/suggestions')
    def suggestions(ident:str):
        return service.suggestions(ident)

    @app.get('/api/jobs/{ident}/context/{subtitle_id}')
    def context(ident:str, subtitle_id:str):
        return service.context(ident,subtitle_id)

    @app.get('/api/jobs/{ident}/review-state')
    def review_state(ident:str):
        from .review import state
        return state(service,ident)

    @app.post('/api/jobs/{ident}/review/bulk')
    def bulk_decisions(ident:str, body:BulkDecision):
        from .review import bulk
        return bulk(service,ident,body.action,body.expected_version,body.operation_id,body.confirmed)

    @app.post('/api/jobs/{ident}/review/restore')
    def restore_decisions(ident:str, body:ReviewConfirmation):
        from .review import restore
        return restore(service,ident,body.expected_version,body.operation_id,body.confirmed)

    @app.post('/api/jobs/{ident}/export/current')
    def export_current_decisions(ident:str, body:ReviewConfirmation):
        from .review import export_current
        return export_current(service,ident,body.expected_version,body.operation_id,body.confirmed)

    @app.put('/api/jobs/{ident}/decisions/{subtitle_id}')
    def decision(ident:str, subtitle_id:str, body:Decision):
        from .review import decision
        return decision(service,ident,subtitle_id,body.action,body.text,body.expected_version)

    @app.post('/api/jobs/{ident}/export')
    def export(ident:str, body:FullExport | None = None):
        from .review import export_full
        return export_full(service,ident,body.expected_version if body else None)

    @app.get('/api/jobs/{ident}/download/{kind}')
    def download(ident:str,kind:Literal['translated','revised','current']):
        if kind=='current':
            from .review import current_path
            path=current_path(service,ident)
        else:
            path=service.download(ident,kind)
        return FileResponse(path,media_type='application/x-subrip',filename=path.name)

    return app
