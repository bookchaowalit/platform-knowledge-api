---
title: solo-knowledge-api — Product Card
doc_id: CARD-solo-knowledge-api
doc_type: product-card
verdict: showcase
version: 0.1
date: 2026-10-11
status: ร่างเพื่อพิจารณา
owner: Product owner (Book Dev)
---

# solo-knowledge-api — Product Card

## 1. สรุปคุณค่าในหนึ่งประโยค

ให้เว็บและ agent ค้นรายการเอกสารของ Solo Empire ผ่าน API อ่านอย่างเดียวจาก snapshot `data/document-catalog.json`

## 2. ปัญหาและผู้ใช้เป้าหมาย

catalog สร้างใน solo-empire แต่ต้องมี service ที่ deploy แยกได้ ผู้ใช้คือ solo-web และ agent

## 3. สิ่งที่ทำได้จริงตอนนี้

- `server.py` อ่าน catalog JSON (path ตั้งได้ด้วย `KNOWLEDGE_API_CATALOG_PATH`)
- `scripts/validate_catalog.py` ตรวจ schema/ความไม่ซ้ำของ path ของ snapshot
- เทสต์ 3 ไฟล์

## 4. หลักฐานสถานะ

| รายการ | ค่า |
|---|---|
| Stack | Python (stdlib server) |
| Commit | 7 ครั้ง (2026-09-09 ถึง 2026-10-11) |
| ซอร์สโค้ด | 6 ไฟล์ / 1302 บรรทัด |
| เทสต์ | 3 ไฟล์ (ไม่ได้รันในงานนี้) |
| CI | ไม่มี workflow |
| Deploy config | `railway.toml` + `Dockerfile` (ทะเบียนระบุ staged) |
| Live URL | ไม่มี |

## 5. คุณค่าและจุดต่าง

- แยก API จาก monorepo
- **[สมมติฐาน]** ไม่มีมูลค่าขาย

## 6. ช่องว่างสำคัญ

- snapshot ต้องถูกเขียนลง repo นี้โดย generator ภายนอก
- ยังไม่ deploy (staged)

## 7. คำตัดสิน

**`showcase`** — ชั้นอ่านบางที่ทำงานได้ แต่ไม่มีมูลค่าเกินกว่าเป็นทางผ่านของ catalog จะเปลี่ยนเป็น `merge` หากรวมกับ solo-knowledge

## 8. ขั้นต่อไป

1. รัน `scripts/validate_catalog.py` ใน CI หรือ task

## การอนุมัติ

| บทบาท | สถานะ | วันที่ | ลงนาม |
|---|---|---|---|
| Product owner (Book Dev) | รอพิจารณา | — | ________ |

## แหล่งข้อมูล

- `README.md`
- `server.py`, `scripts/validate_catalog.py`, `README.md`
