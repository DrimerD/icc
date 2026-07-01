# Налаштування вхідних дзвінків: Carrier/Kamailio → Asterisk → Dograh (ARI)

> Мета: вхідний дзвінок з транку доходить до голосового агента Dograh.
> Схема: `Carrier/Kamailio ──SIP──▶ Asterisk ──ARI events──▶ Dograh` + `Asterisk ──media(WS)──▶ Dograh`

## Як це працює (два WebSocket-з'єднання)

1. **Dograh → Asterisk** (`ari_manager`): Dograh під'єднується до ARI-вебсокета Asterisk (`/ari/events`), слухає події.
2. Вхідний INVITE з транку → діалплан кидає дзвінок у `Stasis(dograh)` → Asterisk шле подію **StasisStart** (стан `Ring`).
3. Dograh читає набраний extension (`dialplan.exten`), знаходить цей номер у вашій ARI-конфігурації, бере призначений `inbound_workflow_id`, перевіряє квоту, створює `workflow_run`.
4. Dograh відповідає на канал і піднімає **externalMedia** канал з `transport=websocket`.
5. **Asterisk → Dograh** (media): Asterisk за `websocket_client.conf` під'єднується до медіа-ендпоінта Dograh і стрімить аудіо (G.711 μ-law). Pipeline (STT→LLM→TTS) починає працювати.

**Важливо для мережі:** Dograh має дотягуватись до ARI-порту Asterisk (8088), **і** Asterisk має дотягуватись до WS-ендпоінта Dograh.

## 0. Передумови

- Asterisk з модулями `chan_websocket` і `res_websocket_client`. Робочі збірки: **Asterisk 22+** або **Asterisk 20 LTS** з цими модулями.
  Перевірка:
  ```bash
  asterisk -rx "module show like chan_websocket"
  asterisk -rx "module show like res_websocket_client"
  # обидва мають бути Running
  ```
- Запущений і доступний інстанс Dograh.
- Транк від оператора або через Kamailio/OpenSIPS, що доходить до Asterisk.

## 1. Конфіги Asterisk

### `ari.conf` — користувач ARI, яким автентифікується Dograh

```ini
[general]
enabled = yes

[dograh]            ; ← це Stasis App Name + ARI username
type = user
read_only = no
password = your_secure_password   ; ← це App Password у Dograh
```

### `http.conf` — увімкнути HTTP-сервер (ARI працює поверх нього)

```ini
[general]
enabled = yes
bindaddr = 0.0.0.0
bindport = 8088
```

### `pjsip.conf` — приймання дзвінків з транку/Kamailio

Мінімальний приклад для **IP-authenticated** транку (оператор/Kamailio шле INVITE на ваш IP):

```ini
[transport-udp]
type = transport
protocol = udp
bind = 0.0.0.0:5060

; Вхідний транк від Kamailio/оператора
[carrier]
type = endpoint
context = from-external          ; ← контекст діалплану нижче
disallow = all
allow = ulaw                     ; ОБОВ'ЯЗКОВО ulaw — externalMedia Dograh працює в μ-law
aors = carrier
identify_by = ip

[carrier]
type = identify                  ; впізнаємо транк за IP джерела
endpoint = carrier
match = 203.0.113.10             ; ← IP вашого Kamailio/оператора

[carrier]
type = aor
contact = sip:203.0.113.10:5060  ; ← адреса Kamailio/оператора
```

> Якщо транк **register-based** (треба реєструватись угору) — реєстрацію зручніше тримати на Kamailio/OpenSIPS, а між Kamailio і Asterisk робити простий IP-trunk як вище. Asterisk для реєстрації не обов'язковий.

### `extensions.conf` — маршрут DID у Stasis-додаток

Конкретний номер:

```ini
[from-external]
exten => 380441234567,1,NoOp(Inbound to ${EXTEN})
 same => n,Stasis(dograh)
 same => n,Hangup()
```

…або патерн, що ловить будь-який номер, який ви зареєструєте в Dograh:

```ini
[from-external]
exten => _X.,1,NoOp(Inbound to ${EXTEN})
 same => n,Stasis(dograh)
 same => n,Hangup()
```

> `dograh` тут має збігатися з ім'ям секції в `ari.conf` і зі **Stasis App Name** у Dograh.

### `websocket_client.conf` — як Asterisk стрімить медіа в Dograh

Self-hosted (внутрішня мережа, без TLS):

```ini
[dograh]
type = websocket_client
uri = ws://your-dograh-host:port/api/v1/telephony/ws/ari
protocols = media
```

Dograh Cloud / HTTPS:

```ini
[dograh]
type = websocket_client
uri = wss://api.dograh.com/api/v1/telephony/ws/ari
protocols = media
tls_enabled = yes                ; обов'язково для wss, навіть зі схемою wss://
ca_list_file = /etc/ssl/certs/ca-certificates.crt
```

> Ім'я секції (`dograh`) — це **WebSocket Client Name**, яке ви вкажете в Dograh.

### Застосувати зміни

```bash
asterisk -rx "ari reload"
asterisk -rx "dialplan reload"
asterisk -rx "module reload res_websocket_client.so"
asterisk -rx "pjsip reload"
# зміни в http.conf вимагають: asterisk -rx "core reload" або рестарт
```

## 2. Налаштування в Dograh

### Крок 1 — створити конфігурацію телефонії

1. Відкрити **/telephony-configurations** → **Add configuration**
2. Провайдер: **Asterisk ARI**
3. Заповнити поля:

| Поле | Значення | Звідки |
| --- | --- | --- |
| **ARI Endpoint URL** | `http://asterisk.example.com:8088` | HTTP-адреса Asterisk (`http.conf`) |
| **Stasis App Name** | `dograh` | секція в `ari.conf` |
| **App Password** | `your_secure_password` | `password` в `ari.conf` |
| **WebSocket Client Name** | `dograh` | секція в `websocket_client.conf` |
| **From Extensions** | *(для вхідних не обов'язково)* | — |

4. **Save Configuration**

### Крок 2 — додати номер і призначити workflow

1. Відкрити щойно створену конфігурацію.
2. У **Phone numbers** додати номер, адреса якого = ваш DID/extension (той, що в діалплані, напр. `380441234567`).
3. Виставити **Inbound workflow** — агент, який має відповідати.
4. Save.

> Додавання номера в Dograh **не змінює** діалплан Asterisk — за маршрутизацію в Stasis відповідає `extensions.conf`. Запис у Dograh лише каже, який workflow запускати для цього extension.

## 3. Тест вхідного дзвінка

1. Подзвонити на ваш DID з іншого телефону.
2. Очікувано: агент Dograh відповідає, чути двостороннє аудіо.
3. Перевірити:
   - Asterisk CLI: `asterisk -rvvv` — має бути StasisStart і externalMedia канал.
   - Логи Dograh — `StasisStart for ext ...`, створення `workflow run`, старт pipeline.
   - Список дзвінків/ранів у Dograh.

## 4. Швидка діагностика

| Симптом | Перевірити |
| --- | --- |
| Dograh не конектиться до ARI | URL/порт 8088 доступний з Dograh; `ari.conf enabled=yes`; `module show like res_ari` = Running; збіг App Name/Password |
| Дзвінок не доходить до Stasis | Діалплан кидає в `Stasis(dograh)`; ім'я app збігається; INVITE взагалі доходить до Asterisk (`pjsip set logger on`) |
| Дзвінок одразу кладеться | Номер доданий у Dograh саме для цієї ARI-конфігурації і має призначений inbound workflow; є квота |
| Немає аудіо | `chan_websocket` Running; `websocket_client.conf` вказує на правильний URI Dograh; WebSocket Client Name збігається; на endpoint `allow=ulaw` |
| TLS-помилки (wss) | `tls_enabled = yes`; правильний `ca_list_file`; коректний hostname/сертифікат |

## 5. Контрольний список

- [ ] `chan_websocket` і `res_websocket_client` = Running
- [ ] `ari.conf`: user `dograh` + пароль
- [ ] `http.conf`: enabled, порт 8088
- [ ] `pjsip.conf`: транк від Kamailio/оператора, `allow=ulaw`
- [ ] `extensions.conf`: DID → `Stasis(dograh)`
- [ ] `websocket_client.conf`: секція `dograh` → WS-ендпоінт Dograh
- [ ] Мережа: Dograh→Asterisk:8088 і Asterisk→Dograh:WS відкриті
- [ ] Dograh: конфігурація Asterisk ARI збережена
- [ ] Dograh: DID доданий як номер + призначений inbound workflow
- [ ] Тестовий дзвінок проходить
```
