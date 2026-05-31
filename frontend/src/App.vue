<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import ToastMessage from './components/ToastMessage.vue'
import ThemeToggle from './components/ThemeToggle.vue'

const route = useRoute()
const showThemeToggle = computed(() => !['login', 'register'].includes(String(route.name)))
</script>

<template>
  <router-view />
  <ToastMessage />
  <ThemeToggle v-if="showThemeToggle" />
</template>
