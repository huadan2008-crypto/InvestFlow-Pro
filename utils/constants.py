"""
COO 邮件完整正文模板（默认稿）。
插值变量（仅替换以下占位符）：{{ticker}}, {{company_name}}, {{price}}, {{warrant_info}}, {{options_text}}, {{deadline_text}}, {{oid_link}}
"""

# 默认邮件主题（可自行在界面修改；仍支持 {{ticker}} / {{company_name}}）
COO_DISTRIBUTION_DEFAULT_SUBJECT = "[EDE/{{ticker}}] 定增材料与认购意向 — {{company_name}}"

# 用户提供的完整正文（除上述占位符外保持原样）
COO_DISTRIBUTION_EMAIL_BODY_TEMPLATE = r"""尊敬的投资人：
 
您好。{{ticker}}的Presentation请您参见附件。
 
{{ticker}} 定增价格${{price}}/股。{{warrant_info}}
 
{{ticker}} 定增每位投资人有认购额度可以选择：{{options_text}}。
 
如果您想参与此次定增，请点击下方专属链接提交您的认购意向：
🔗 [点击此处提交认购意向]({{oid_link}})
(如果您更习惯邮件回复，请尽快回复此邮件并提供姓名、额度和电话。)
 
因为名额有限，公司会安排我们懿德联动专户投资人和value fund 基金投资人优先参与。
 
另外，此次定增Close时间比较紧急，请您务必在{{deadline_text}}前回复您的订购额度。
 
如果您成功申请了此次定增，我们会发出确认邮件跟您单独联系，如果在2周内您没有收到任何确认邮件，基本表示此次认购已经额满，您没有获得相应的额度。鉴于工作量的巨大，没有获得相应额度的投资人我们一般不会另行通知，望见谅。
 
非常感谢您的信任以及参与，如果有任何问题，欢迎随时与我们联系！
 
谢谢
 
**注1：懿德公司的所有定向增发投资项目只针对accredited investor开放
**注2：本文档中包含的信息由公司提供。EDE Asset Management Inc.不保证该信息仅对经认可的投资者是真实准确的，并且本文档中包含的信息不构成财务，投资建议，投资咨询或其他建议。敬请投资者注意风险，并谨慎决策。
**Note: The information contained in this document is provided by the company and EDE Asset Management Inc. does not guarantee that the information is true and accurate for the information of accredited investors only, and the information contained in this document does not constitute financial, investment advice, investment consulting or other advice. Investors are kindly advised to take full care of risk and make prudent decisions
 

Aaron Zhong
COO&CSO
T: 416-238-2598 | C: 416-577-6530
E: aaron.zhong@edeasset.com

www.edeasset.com
8 King Street East, Suite 610, Toronto, ON M5C 1B5

(此处包含后面所有的 Confidentiality 声明...)
"""

# 与 mail_templates.json 结构一致；`data/mail_templates.json` 损坏或丢失时由 Distribution 回退加载
DEFAULT_MAIL_TEMPLATE = {
    "active_template_id": "WML",
    "templates": {
        "WML": {
            "name": "WML 默认定增",
            "subject": COO_DISTRIBUTION_DEFAULT_SUBJECT,
            "body": COO_DISTRIBUTION_EMAIL_BODY_TEMPLATE,
            "content": COO_DISTRIBUTION_EMAIL_BODY_TEMPLATE,
        }
    },
}
