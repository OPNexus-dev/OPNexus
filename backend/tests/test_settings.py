import pytest

@pytest.mark.asyncio
async def test_settings_theme_and_language_persist_and_reject_unknown_values(client,anon_client):
    assert (await anon_client.get('/api/settings')).status_code==401
    initial=(await client.get('/api/settings')).json()
    assert initial['theme']=='system' and initial['language'] is None and initial['deploy_confirm_window_seconds']>0
    response=await client.put('/api/settings',json={'theme':'dark','language':'en'})
    assert response.status_code==200 and response.json()['theme']=='dark' and response.json()['language']=='en'
    assert (await client.get('/api/settings')).json()['theme']=='dark'
    assert (await client.get('/api/settings')).json()['language']=='en'
    response=await client.put('/api/settings',json={'language':'de'})
    assert response.status_code==200 and response.json()['theme']=='dark' and response.json()['language']=='de'
    assert (await client.put('/api/settings',json={'theme':'invalid'})).status_code==422
    assert (await client.put('/api/settings',json={'theme':'dark','language':'fr'})).status_code==422
    assert (await client.get('/api/settings')).json()['theme']=='dark'


@pytest.mark.asyncio
async def test_language_stays_unset_until_chosen(client):
    # Saving only the theme must not pin a language: unset means "follow the browser".
    response=await client.put('/api/settings',json={'theme':'dark'})
    assert response.status_code==200 and response.json()['language'] is None
    assert (await client.get('/api/settings')).json()['language'] is None
    response=await client.put('/api/settings',json={'language':'en'})
    assert response.json()['language']=='en'
    assert (await client.put('/api/settings',json={'theme':'light'})).json()['language']=='en'
