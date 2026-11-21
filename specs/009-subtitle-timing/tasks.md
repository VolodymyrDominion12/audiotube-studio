# 009 · Задачі

Тест першим → код → `pytest -q`. `[P]` — незалежні.

- [x] **T1** Ключі конфігу + `SubtitleRules.from_config` (валідація min < max)
- [x] **T2** Поділ довгого сегмента за часом — SUB-AC1
- [x] **T3** Пріоритет точок поділу — SUB-AC2
- [x] **T4** [P] Баланс рядків — SUB-AC3
- [x] **T5** Вирівнювання часу: cps у тишу, лічильник щільних, закриття коротких пауз — SUB-AC4, AC5, AC6
- [x] **T6** Властивості на випадкових транскриптах + детермінованість — SUB-AC7, AC8; наявні тести — SUB-AC9
- [x] **T7** Підключити правила в `transcribe` і `Pipeline._subtitles_path` (SUB-R10), журнал «щільних»
- [x] **T8** `rebuild_subtitles` + CLI `subtitles`: створення та ідемпотентність — SUB-AC10
- [x] **T9** `--force` з `.bak` і без змін — SUB-AC11
- [x] **T10** [P] `--dry-run` — SUB-AC12
- [x] **T11** [P] Задачі з `video_id`, без транскрипту, невідомий id — SUB-AC13, AC14
- [x] **T12** docs/04, `config.example.yaml`, README, `specs/README.md`, статус → implemented
