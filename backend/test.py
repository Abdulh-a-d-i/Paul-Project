from src.utils.mail_management import Send_Mail

import asyncio

send_mail = Send_Mail()
async def main():
    response = await send_mail.send_email_call_details_async("muhammadahad764@gmail.com","hi my name is techbot i am test you.","https://www.google.com")
    print(response)

asyncio.run(main())