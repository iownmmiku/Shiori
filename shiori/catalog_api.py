"""Model-source/catalog routes; the application's common auth protects them."""
from fastapi import Body

from .model_catalog import ModelCatalog


def install_catalog(app, settings, provider, store=None):
    if getattr(app.state, 'model_catalog', None) is not None:
        return app.state.model_catalog
    catalog = ModelCatalog(settings, provider, store)
    app.state.model_catalog = catalog

    @app.get('/api/model-providers')
    async def list_sources():
        return catalog.snapshot()

    @app.post('/api/model-providers/discover')
    async def discover_form(payload: dict = Body(...)):
        return await catalog.discover(payload)

    @app.post('/api/model-providers')
    async def add_source(payload: dict = Body(...)):
        return catalog.save_source(payload)

    @app.put('/api/model-providers/{source_id}')
    async def update_source(source_id: str, payload: dict = Body(...)):
        return catalog.save_source(payload, source_id)

    @app.delete('/api/model-providers/{source_id}')
    async def remove_source(source_id: str):
        return catalog.delete_source(source_id)

    @app.post('/api/model-providers/{source_id}/discover')
    async def discover_source(source_id: str, payload: dict = Body(default={})):
        return await catalog.discover(payload, source_id)

    @app.post('/api/model-catalog/models')
    async def add_models(payload: dict = Body(...)):
        return catalog.add_models(payload)

    @app.patch('/api/model-catalog/models/{model_id}')
    async def update_model(model_id: str, payload: dict = Body(...)):
        return catalog.update_model(model_id, payload)

    @app.delete('/api/model-catalog/models/{model_id}')
    async def remove_model(model_id: str):
        return catalog.delete_model(model_id)

    @app.put('/api/model-catalog/routing')
    async def update_routing(payload: dict = Body(...)):
        return catalog.update_routing(payload)

    @app.put('/api/model-catalog/embedding')
    async def configure_embedding(payload: dict = Body(...)):
        return catalog.configure_embedding(payload)

    @app.post('/api/model-catalog/embedding/test')
    async def test_embedding(payload: dict = Body(...)):
        return await catalog.test_embedding(payload.get('model_id'), activate=payload.get('activate', False))

    return catalog
