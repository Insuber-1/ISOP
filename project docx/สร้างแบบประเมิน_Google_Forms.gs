/**
 * สร้างแบบประเมินการใช้งาน Intelligent Server Operations Platform
 * วิธีใช้: วางไฟล์นี้ในโครงการใหม่ที่ script.google.com แล้วเรียกใช้
 * createProjectAssessmentForm() เพียงครั้งเดียว
 * แบบฟอร์มจะถูกสร้างแบบยังไม่เผยแพร่ เพื่อให้ตรวจทานก่อนเปิดรับคำตอบ
 * เรียกซ้ำจะเปิดแบบฟอร์มเดิม ไม่สร้างสำเนาใหม่
 */
function createProjectAssessmentForm() {
  const properties = PropertiesService.getScriptProperties();
  const savedId = properties.getProperty('PROJECT_ASSESSMENT_FORM_ID');

  if (savedId) {
    const existing = FormApp.openById(savedId);
    Logger.log('พบแบบฟอร์มที่สร้างไว้แล้ว');
    Logger.log('ลิงก์แก้ไข: ' + existing.getEditUrl());
    Logger.log('ลิงก์สำหรับผู้ตอบ: ' + existing.getPublishedUrl());
    return;
  }

  const form = FormApp.create(
    'แบบประเมินการใช้งานระบบ Intelligent Server Operations Platform',
    false
  );
  form
    .setDescription(
      'แบบสอบถามสำหรับผู้ที่ได้ทดลองใช้ระบบปฏิบัติการและบริหารจัดการเครื่องแม่ข่าย ' +
      'โปรดตอบตามประสบการณ์จริง การตอบเป็นไปโดยสมัครใจ ไม่ต้องระบุชื่อ ' +
      'และจะไม่นำอีเมลผู้ตอบมาเก็บ ใช้คำตอบเพื่อสรุปผลในภาพรวมของโครงงานเท่านั้น ' +
      'ข้อที่ยังไม่ได้ทดลองใช้สามารถเลือก “ไม่เคยใช้/ไม่เกี่ยวข้อง”'
    )
    .setCollectEmail(false)
    .setConfirmationMessage('ขอบคุณสำหรับความคิดเห็นและข้อเสนอแนะ');

  form.addSectionHeaderItem()
    .setTitle('ส่วนที่ 1 ข้อมูลทั่วไป')
    .setHelpText('ไม่ต้องระบุชื่อหรือข้อมูลที่ใช้ระบุตัวบุคคล');

  form.addMultipleChoiceItem()
    .setTitle('1. บทบาทของผู้ตอบ')
    .setChoiceValues([
      'ผู้ดูแลระบบ/ไอที',
      'ผู้พัฒนาระบบ',
      'นักศึกษา',
      'อื่น ๆ'
    ])
    .setRequired(false);

  form.addMultipleChoiceItem()
    .setTitle('2. ประสบการณ์ด้านการดูแลระบบหรือ Docker')
    .setChoiceValues([
      'ต่ำกว่า 1 ปี',
      '1–3 ปี',
      'มากกว่า 3 ปี',
      'ไม่มีประสบการณ์'
    ])
    .setRequired(false);

  form.addPageBreakItem()
    .setTitle('ส่วนที่ 2 ความคิดเห็นต่อการใช้งาน')
    .setHelpText(
      'ให้คะแนนแต่ละข้อ: 1 = ไม่เห็นด้วยอย่างยิ่ง, 2 = ไม่เห็นด้วย, ' +
      '3 = ไม่แน่ใจ, 4 = เห็นด้วย, 5 = เห็นด้วยอย่างยิ่ง, ' +
      'ไม่เคยใช้/ไม่เกี่ยวข้อง = ยังไม่ได้ทดลองใช้หรือไม่เกี่ยวกับงานที่ทดลอง'
    );

  const statements = [
    'หน้าหลักแสดงค่า CPU, RAM และพื้นที่จัดเก็บได้เข้าใจง่าย',
    'กราฟและประวัติค่าทรัพยากรช่วยให้ติดตามการเปลี่ยนแปลงได้สะดวก',
    'สถานะและการแจ้งเตือนช่วยให้สังเกตค่าที่ควรตรวจสอบได้ชัดเจน',
    'การนำเข้าไฟล์ Docker Compose และเลือกรายการบริการทำได้เข้าใจง่าย',
    'ข้อมูลสถานะและ logs ของ container มีประโยชน์ต่อการตรวจสอบบริการ',
    'การสั่ง start, stop หรือ restart แสดงผลและข้อความตอบกลับชัดเจน',
    'การตั้งค่า CPU/RAM ของ container เข้าใจง่ายและควบคุมได้',
    'การเข้าสู่ระบบเว็บแดชบอร์ดและใช้งานหน้าจอเว็บทำได้สะดวก',
    'การสำรองโฟลเดอร์โครงการเป็น ZIP มีขั้นตอนที่เข้าใจง่าย',
    'ข้อความแจ้งข้อผิดพลาดช่วยให้ทราบขั้นตอนตรวจสอบต่อไป',
    'โดยรวม ฉันสามารถเรียนรู้วิธีใช้งานระบบได้โดยไม่ยุ่งยาก',
    'โดยรวม ระบบมีประโยชน์ต่อการติดตามและจัดการเครื่องแม่ข่ายตามขอบเขตที่ระบุ'
  ];

  form.addGridItem()
    .setTitle('โปรดเลือกระดับความคิดเห็นหนึ่งระดับในแต่ละข้อ')
    .setRows(statements)
    .setColumns([
      '1 ไม่เห็นด้วยอย่างยิ่ง',
      '2 ไม่เห็นด้วย',
      '3 ไม่แน่ใจ',
      '4 เห็นด้วย',
      '5 เห็นด้วยอย่างยิ่ง',
      'ไม่เคยใช้/ไม่เกี่ยวข้อง'
    ])
    .setRequired(false);

  form.addPageBreakItem()
    .setTitle('ส่วนที่ 3 ประสบการณ์ทดลองใช้และข้อเสนอแนะ');

  form.addCheckboxItem()
    .setTitle('งานที่ได้ทดลองทำ (เลือกได้มากกว่าหนึ่งข้อ)')
    .setChoiceValues([
      'ดูค่า/กราฟทรัพยากร',
      'ตรวจสถานะหรือ logs ของ container',
      'สั่งจัดการ container',
      'ใช้เว็บแดชบอร์ด',
      'สำรองข้อมูล',
      'อื่น ๆ'
    ])
    .setRequired(false);

  form.addParagraphTextItem()
    .setTitle('ส่วนใดใช้งานได้สะดวกหรือเป็นประโยชน์มากที่สุด เพราะเหตุใด?')
    .setRequired(false);

  form.addParagraphTextItem()
    .setTitle('พบปัญหา ความสับสน หรือข้อควรระวังใดระหว่างทดลองใช้หรือไม่? โปรดระบุ')
    .setRequired(false);

  form.addParagraphTextItem()
    .setTitle('มีข้อเสนอแนะเพื่อปรับปรุงระบบหรือไม่?')
    .setRequired(false);

  properties.setProperty('PROJECT_ASSESSMENT_FORM_ID', form.getId());
  Logger.log('สร้างแบบฟอร์มฉบับร่างแล้ว ตรวจทานและเผยแพร่จากหน้าแก้ไขเมื่อพร้อม');
  Logger.log('ลิงก์แก้ไข: ' + form.getEditUrl());
  Logger.log('ลิงก์สำหรับผู้ตอบ: ' + form.getPublishedUrl());
}
