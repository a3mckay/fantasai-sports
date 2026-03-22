import { initializeApp } from 'firebase/app'
import {
  getAuth,
  GoogleAuthProvider,
  OAuthProvider,
  FacebookAuthProvider,
} from 'firebase/auth'

// Firebase config (public — safe to bundle)
const firebaseConfig = {
  apiKey: 'AIzaSyC-tjMdlY56uowPiyH7GKGX8nHyyVxHY6s',
  authDomain: 'fantasaisports-fantasy-gm.firebaseapp.com',
  projectId: 'fantasaisports-fantasy-gm',
  storageBucket: 'fantasaisports-fantasy-gm.firebasestorage.app',
  messagingSenderId: '1011872408182',
  appId: '1:1011872408182:web:ba5e96f3d4abee25007557',
  measurementId: 'G-MM8TKESYQP',
}

const app = initializeApp(firebaseConfig)
export const auth = getAuth(app)

// Providers
export const googleProvider = new GoogleAuthProvider()
googleProvider.setCustomParameters({ prompt: 'select_account' })

export const appleProvider = new OAuthProvider('apple.com')
appleProvider.addScope('email')
appleProvider.addScope('name')

export const facebookProvider = new FacebookAuthProvider()
facebookProvider.addScope('email')

export default app
