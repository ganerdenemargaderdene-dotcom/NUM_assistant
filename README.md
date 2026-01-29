# NUM Assistant (Unified Rasa Backend)

Энэ төсөл нь дараах модулиудыг нэгтгэсэн нэг Rasa backend юм:
- Сургалтын төлбөр тооцоолол
- GPA (голч) тооцоолол
- МУИС байр, төвүүдийн байршил
- Албан бичгийн загварууд (чөлөө, өвчтэй, дүн асуух, W/I)

## Ажиллуулах

1) Rasa суулгасан байх шаардлагатай.
2) Action server асаана:

```bash
rasa run actions
```

3) Rasa server асаана:

```bash
rasa run --enable-api --cors "*"
```

## Тест хийх жишээ

```bash
curl -X POST http://localhost:5005/webhooks/rest/webhook \
  -H "Content-Type: application/json" \
  -d '{"sender":"test","message":"төлбөр бодоорой"}'
```
