from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
password = "F4pm3nd024!!"
print(pwd_context.hash(password))
