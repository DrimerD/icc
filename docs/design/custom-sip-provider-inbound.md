# Дизайн: власний SIP-провайдер (вхідні дзвінки) для Dograh

> Статус: чернетка дизайну • Напрямок: **тільки вхідні** • Інфраструктура: **Kamailio/OpenSIPS або карьерний SIP-транк**

## 1. Головний архітектурний факт

Dograh **не містить SIP-стека**. У його моделі «провайдер телефонії» — це адаптер, який:

1. приймає **аудіо по WebSocket** і запускає голосовий pipeline (STT → LLM → TTS);
2. зіставляє вхідний дзвінок з потрібним workflow.

Сам SIP (сигналізація INVITE/BYE) і медіа (RTP) обробляє **зовнішня інфраструктура**, яка потім стрімить аудіо в WebSocket Dograh. Це підтверджують усі наявні провайдери: Twilio/Plivo/Vonage стрімлять медіа по WS, Cloudonix — теж («Cloudonix WebSocket is compatible with Twilio»), а ARI використовує `chan_websocket` Asterisk для external media.

**Наслідок для вас:** Kamailio та OpenSIPS — це SIP-**проксі/балансувальники**. Вони маршрутизують сигналізацію, але **самі не термінують медіа (RTP)** і не вміють конвертувати RTP↔WebSocket. Тому одного Kamailio/OpenSIPS недостатньо — між ними (чи карьерним транком) і Dograh **обов'язково потрібен медіа-елемент** (B2BUA / медіа-сервер), який:

- відповість на INVITE і термінує RTP;
- відкриє WebSocket до Dograh і передасть аудіо в очікуваному форматі;
- передасть метадані дзвінка (на який номер дзвонили, хто дзвонив), щоб Dograh знайшов потрібний workflow.

Цей елемент далі називаємо **SIP↔WS Gateway**.

```
PSTN/Carrier ──SIP──▶ Kamailio/OpenSIPS ──SIP──▶ [SIP↔WS Gateway] ──WS(аудіо)──▶ Dograh
   (транк)            (маршрутизація)           (термінація RTP,        (pipeline + workflow)
                                                  міст RTP↔WebSocket)
```

## 2. Два варіанти реалізації

### Варіант A (рекомендований для швидкого старту): Asterisk як gateway + наявний провайдер `ari`

Поставити Asterisk **за** Kamailio/OpenSIPS як медіа-сервер. Kamailio маршрутизує вхідні INVITE на Asterisk, Asterisk через `Stasis` + `chan_websocket` стрімить external media в Dograh.

- **Коду в Dograh писати не треба** — провайдер `ari` уже існує (`api/services/telephony/providers/ari/`).
- Потрібна лише конфігурація Asterisk (`ari.conf`, `http.conf`, `extensions.conf`, `websocket_client.conf`) — усе описано в `docs/integrations/telephony/asterisk-ari.mdx`.
- Вхідний дзвінок приходить як **StasisStart** на ARI WebSocket; Dograh зіставляє набраний extension з workflow.
- Аудіо: G.711 μ-law 8 кГц.

Мінус: ще один компонент в інфраструктурі (Asterisk). Але це найнадійніший і найшвидший шлях, який не потребує підтримки власного коду.

### Варіант B (повний кастом): новий провайдер + власний SIP↔WS Gateway

Якщо Asterisk небажаний (наприклад, карьерний транк заходить прямо у ваш кастомний софтсвіч, або ви не хочете тягнути Asterisk), будуємо:

1. **Новий self-registering провайдер** у Dograh (мінімум коду — повторно використовуємо протокол Twilio Media Streams);
2. **Власний SIP↔WS Gateway** у вашій інфраструктурі, що термінує SIP/RTP і говорить з Dograh по WS у форматі Twilio Media Streams.

Решта документа детально описує **Варіант B**, бо саме він означає «власний провайдер».

> Рекомендація: якщо немає жорсткої вимоги уникати Asterisk — почати з Варіанта A (нуль коду), а Варіант B розглядати, коли потрібен повний контроль над SIP-стеком.

## 3. Контракт gateway ↔ Dograh (Варіант B)

Найдешевший шлях — зробити gateway сумісним з **Twilio Media Streams**, бо Dograh уже має `TwilioFrameSerializer` (pipecat) і робочий WS-handshake. Тоді на боці Dograh код провайдера зводиться майже до конфігу.

Вхідний потік повністю повторює наявний диспетчер `POST /api/v1/telephony/inbound/run` (`api/routes/telephony.py`):

1. **HTTP-вебхук (gateway → Dograh).** Отримавши INVITE, gateway робить `POST /api/v1/telephony/inbound/run` з тілом, що містить принаймні:
   - `To` — набраний номер/DID (за ним підбирається workflow);
   - `From` — номер абонента;
   - `CallSid` — унікальний ID дзвінка від gateway;
   - `account_id` — ідентифікатор вашого gateway/акаунта (для зіставлення конфігурації);
   - заголовок для детекції провайдера (напр. `X-Dograh-Provider: sip_gateway`) і заголовок підпису `X-Dograh-Signature` (HMAC-SHA256 над тілом).
2. **Маршрутизація (Dograh).** Диспетчер викликає `can_handle_webhook` → `parse_inbound_webhook` → `find_inbound_route_by_account` (зіставляє конфіг і номер) → перевіряє `inbound_workflow_id` на номері → `verify_inbound_signature` → створює `workflow_run` + перевіряє квоту.
3. **Відповідь (Dograh → gateway).** `start_inbound_stream` повертає **JSON** з WebSocket-URL:
   ```json
   { "websocket_url": "wss://<backend>/api/v1/telephony/ws/<workflow_id>/<user_id>/<workflow_run_id>" }
   ```
4. **WebSocket (gateway → Dograh).** Gateway відкриває цей WS і шле повідомлення у форматі Twilio Media Streams:
   - `{"event":"connected", ...}`
   - `{"event":"start","start":{"streamSid":"...","callSid":"..."}}`
   - `{"event":"media","media":{"payload":"<base64 μ-law 8kHz>"}}` (двосторонньо)
   - `{"event":"stop", ...}`
   Dograh у `websocket_endpoint` читає `provider` з `initial_context` запущеного run і делегує в `handle_websocket` провайдера, який і запускає `run_pipeline_telephony`.

**Аудіо-формат:** G.711 μ-law, 8 кГц, base64 у полі `media.payload` (як Twilio). Це дозволяє перевикористати `TwilioFrameSerializer` без змін.

## 4. Що реалізувати на боці Dograh (Варіант B)

Згідно з `api/services/telephony/providers/AGENTS.md`, новий провайдер — це **окремий пакет** + **рівно два рядки** поза ним. Назвемо провайдер `sip_gateway`.

```
api/services/telephony/providers/sip_gateway/
├── __init__.py      # ProviderSpec + register()
├── config.py        # Pydantic Request/Response, provider: Literal["sip_gateway"]
├── provider.py      # SipGatewayProvider(TelephonyProvider)
├── transport.py     # create_transport(...) -> FastAPIWebsocketTransport
└── serializers.py   # re-export TwilioFrameSerializer
```

### 4.1 `config.py`

```python
from typing import List, Literal
from pydantic import BaseModel, Field

class SipGatewayConfigurationRequest(BaseModel):
    provider: Literal["sip_gateway"] = Field(default="sip_gateway")
    account_id: str = Field(..., description="Ідентифікатор вашого gateway")
    shared_secret: str = Field(..., description="Секрет для HMAC-підпису вебхуків")
    from_numbers: List[str] = Field(default_factory=list)

class SipGatewayConfigurationResponse(BaseModel):
    provider: Literal["sip_gateway"] = Field(default="sip_gateway")
    account_id: str
    shared_secret: str   # маскується при читанні
    from_numbers: List[str]
```

### 4.2 `provider.py` — реалізація інтерфейсу `TelephonyProvider`

Для **тільки вхідних** дзвінків реально важливі лише кілька методів; решта — заглушки.

| Метод | Що робить |
| --- | --- |
| `can_handle_webhook(data, headers)` | `True`, якщо є заголовок `X-Dograh-Provider: sip_gateway` (детекція в `_detect_provider`) |
| `parse_inbound_webhook(data)` | Будує `NormalizedInboundData(to_number, from_number, call_id, account_id, direction="inbound")` |
| `validate_account_id(config, account_id)` | Порівнює `account_id` з вебхука зі збереженим у конфігу |
| `verify_inbound_signature(url, data, headers, body)` | HMAC-SHA256(body, shared_secret) == `X-Dograh-Signature`; якщо підпис присутній і невірний → `False` |
| `start_inbound_stream(websocket_url, ...)` | Повертає `JSONResponse({"websocket_url": websocket_url})` |
| `handle_websocket(ws, workflow_id, user_id, run_id)` | Twilio-сумісний handshake (`connected`→`start`), дістати `call_id` з `gathered_context`, викликати `run_pipeline_telephony(..., transport_kwargs={"call_id":..., "stream_sid":...})` — практично копія `CloudonixProvider.handle_websocket` |
| `generate_error_response` / `generate_validation_error_response` | JSON-помилка (як в `ari`) |
| `supports_transfers()` | `False` |
| `initiate_call`, `get_call_status`, `get_call_cost`, `get_available_phone_numbers`, `get_webhook_response`, `verify_webhook_signature`, `parse_status_callback`, `transfer_call` | Заглушки / `NotImplementedError` (вихідні не потрібні) |

> `handle_websocket` і весь WS-handshake можна практично скопіювати з `providers/cloudonix/provider.py`, бо протокол Twilio Media Streams ідентичний.

### 4.3 `transport.py`

Майже копія `providers/cloudonix/transport.py`, але серіалайзер — `TwilioFrameSerializer`:

```python
config = await load_credentials_for_transport(
    organization_id, telephony_configuration_id, expected_provider="sip_gateway",
)
serializer = TwilioFrameSerializer(stream_sid=stream_sid, call_sid=call_id)
# FastAPIWebsocketTransport з audio_in/out 8000, mixer, serializer
```

### 4.4 `__init__.py` — `ProviderSpec`

```python
SPEC = ProviderSpec(
    name="sip_gateway",
    provider_cls=SipGatewayProvider,
    config_loader=_config_loader,            # чистий reshape dict
    transport_factory=create_transport,
    transport_sample_rate=8000,
    config_request_cls=SipGatewayConfigurationRequest,
    config_response_cls=SipGatewayConfigurationResponse,
    ui_metadata=ProviderUIMetadata(
        display_name="SIP Gateway",
        fields=[
            ProviderUIField(name="account_id", label="Account ID", type="text", sensitive=False),
            ProviderUIField(name="shared_secret", label="Shared Secret", type="password", sensitive=True),
            ProviderUIField(name="from_numbers", label="DIDs", type="string-array"),
        ],
    ),
    account_id_credential_field="account_id",  # для зіставлення вхідних вебхуків
)
register(SPEC)
```

### 4.5 Два рядки поза пакетом

1. `api/services/telephony/providers/__init__.py` — додати `sip_gateway` у список імпортів (реєстрація на import-time).
2. `api/schemas/telephony_config.py` — додати `SipGatewayConfigurationRequest` у `Union` дискримінованого `TelephonyConfigRequest` і `Optional[SipGatewayConfigurationResponse]` у `TelephonyConfigurationResponse`.

**Жодних міграцій БД, змін `factory.py`, `routes/telephony.py` чи фронтенду** — форма конфігурації рендериться з `ui_metadata` автоматично.

## 5. SIP↔WS Gateway (ваша інфраструктура, Варіант B)

Це окремий сервіс, який ви розгортаєте у себе. Варіанти технологій:

- **FreeSWITCH** з `mod_audio_fork` / `mod_websocket` (стрімить медіа у WS) — найближче «з коробки».
- **drachtio + rtpengine** (B2BUA на Node.js, контроль SIP) + окремий конвертер RTP→WS.
- Власний сервіс на **Go** (`emiago/sipgo` + RTP) або **Python** (`aiortc`/`pjsua2`/`baresip`), який термінує SIP/RTP і шле кадри у WS у форматі Twilio Media Streams.

Обов'язки gateway:
1. Прийняти INVITE від Kamailio/OpenSIPS, відповісти 200 OK, термінувати RTP (G.711 μ-law).
2. `POST /inbound/run` з метаданими (`To`, `From`, `CallSid`, `X-Dograh-Provider`, `X-Dograh-Signature`).
3. Отримати `websocket_url`, відкрити WS, надіслати `connected`/`start`, стрімити media в обидва боки.
4. На `BYE` / завершення WS — коректно закрити дзвінок.

> Kamailio/OpenSIPS лишаються попереду для маршрутизації, реєстрації транку, балансування та безпеки (TLS/SRTP, ACL). Медіа вони не чіпають (або проксіюють через rtpengine, що для нашої схеми не обов'язково).

## 6. Послідовність вхідного дзвінка (Варіант B)

```
Абонент → Carrier → Kamailio → Gateway:  INVITE
Gateway → Dograh:                          POST /inbound/run  (To, From, CallSid, signature)
Dograh:                                    detect → parse → match config+номер → verify HMAC
                                           → створити workflow_run → перевірити квоту
Dograh → Gateway:                          200 { websocket_url }
Gateway:                                   200 OK абоненту, термінує RTP
Gateway → Dograh:                          WS connect → connected → start → media…
Dograh:                                    run_pipeline_telephony (STT/LLM/TTS) ↔ media
Абонент ←→ Gateway ←→ Dograh:              двостороннє аудіо
Абонент/Dograh → BYE/stop:                 завершення дзвінка
```

## 7. План впровадження (Варіант B)

1. **Каркас провайдера.** Скопіювати `providers/cloudonix/` → `providers/sip_gateway/`, спростити до вхідних; серіалайзер — `TwilioFrameSerializer`.
2. **Конфіг + реєстрація.** `config.py`, `__init__.py` зі `SPEC`; додати два рядки поза пакетом.
3. **HMAC-підпис.** Реалізувати `verify_inbound_signature` (HMAC-SHA256 над raw body).
4. **Тести** (`api/tests/telephony/sip_gateway/`): `validate_config`, `can_handle_webhook`, `parse_inbound_webhook`, перевірка підпису (валідний/невалідний), форма метаданих.
5. **Локальний прогін.** Зберегти конфіг через UI `/telephony-configurations`, призначити inbound workflow на DID, зімітувати `POST /inbound/run` + WS-клієнт (можна заглушкою на Python) → переконатися, що pipeline стартує.
6. **Gateway (інфра).** Реалізувати/налаштувати SIP↔WS bridge; протестувати реальний дзвінок через Kamailio.
7. **Безпека.** TLS на WS (`wss://`), HMAC на вебхуках, ACL на Kamailio, обмеження джерел вебхука.

## 8. Безпека та якість

- **Підпис вебхуків:** обов'язковий HMAC-SHA256; `verify_inbound_signature` повертає `False`, якщо підпис присутній і невірний.
- **Аудіо:** G.711 μ-law 8 кГц (sample rate в `ProviderSpec.transport_sample_rate=8000`).
- **Маскування секретів:** `shared_secret` з `sensitive=True` — маскується при читанні, відновлюється з `preserve_masked_fields` при повторному збереженні.
- **Мультиконфіг:** `account_id_credential_field="account_id"` дозволяє кілька gateway-конфігів в одній організації.

## 9. Відкриті питання (треба уточнити перед кодом)

1. **Чи прийнятний Asterisk** (Варіант A, нуль коду) — чи потрібен саме власний провайдер (Варіант B)?
2. **Який софт для gateway** плануєте: FreeSWITCH, drachtio, власний сервіс? Це визначає формат WS, який зручніше підтримати.
3. **Аудіо-кодек на транку:** гарантовано G.711 μ-law, чи можливий alaw/opus (впливає на ресемплінг/транскодинг у gateway)?
4. **Як ідентифікувати організацію/конфіг** на вхідному дзвінку: за DID (`To`) і `account_id` gateway — достатньо, чи є кілька тенантів за одним gateway?
5. **Self-hosted чи Dograh Cloud** — від цього залежить `backend_endpoint` у WS-URL і мережеві доступи.
```
