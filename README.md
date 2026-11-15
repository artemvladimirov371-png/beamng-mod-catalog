# BeamNG Mod Catalog

Автоматически обновляемый каталог машин и карт для **BeamNG Mod Installer**. Источник — ModLand. Приложение загружает этот каталог по сети, поэтому новые моды появляются без обновления самого установщика.

## Публичные адреса

- Каталог: `https://artemvladimirov371-png.github.io/beamng-mod-catalog/catalog.json`
- Контрольная сумма: `https://artemvladimirov371-png.github.io/beamng-mod-catalog/catalog.sha256`
- Страница каталога: `https://artemvladimirov371-png.github.io/beamng-mod-catalog/`

## Как это работает

1. GitHub Actions запускается каждые 6 часов (и вручную через `workflow_dispatch`).
2. `tools/modland_sync.py` проверяет авторов ModLand из `tools/catalog_sources.json`.
3. Новые машины и карты добавляются в `tools/catalog_source.json`. Для машин конфигурации читаются из ZIP через HTTP Range — полный архив на сервер не скачивается.
4. Обложки и галереи сохраняются в `catalog_site/assets/mods/` в размере 1080×1080 WebP, миниатюры — 640×640.
5. `tools/build_catalog.py` собирает `catalog_site/catalog.json`, считает SHA-256 и синхронизирует картинки.
6. Изменения коммитятся в `main`, сайт публикуется на GitHub Pages.

## Проверки перед публикацией

- обязательные поля записи и поддерживаемая категория (`car` / `map`);
- наличие ссылки на скачивание;
- точный размер архива и непустой список конфигураций для машин;
- минимум одна обложка;
- карантин проблемных страниц в `tools/pending.json` с причиной;
- не более 20 новых модов за один запуск;
- SHA-256 от фактических байтов `catalog.json`.

## Структура

```
.github/workflows/catalog.yml   обновление и деплой Pages
backend/                        Range-чтение ZIP и резолверы ModLand/ModsFire
tools/catalog_sources.json      авторы, категории и лимиты
tools/catalog_source.json       источник правды каталога
tools/modland_sync.py           сканер и обогащение
tools/build_catalog.py          сборка сайта и локального каталога
tools/pending.json              карантин
catalog_site/                   готовый каталог для GitHub Pages
```

## Локальный запуск

```bash
python -m pip install -r requirements.txt
python tools/modland_sync.py --dry-run --verbose
python tools/modland_sync.py --verbose
```

Проверка одной страницы:

```bash
python tools/modland_sync.py --page "https://www.modland.net/beamng.drive-mods/bmw-m5-g90g99.html" --dry-run
```

Обновление картинок:

```bash
python tools/modland_sync.py --refresh-images --verbose
```

## Настройка

Авторы, категории, задержка запросов и лимит новых записей задаются в `tools/catalog_sources.json`. По умолчанию включена автоматическая публикация (`auto_publish: true`).

## GitHub Pages

В настройках репозитория: **Settings → Pages → Build and deployment → Source: GitHub Actions**. Workflow сам публикует содержимое `catalog_site/`.
