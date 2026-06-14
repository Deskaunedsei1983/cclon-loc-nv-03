# Dockerfile fuer das fragments-Repo (es bringt KEINE eigene mit).
# Wird nach dem Klonen nach ./fragments/app/Dockerfile kopiert (siehe README.md).
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]
