from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('pesquisas/', views.pesquisas, name='pesquisas'),
    path('contatos/', views.contatos, name='contatos'),
    path('contatos/<int:pk>/editar/', views.contato_editar, name='contato_editar'),
    path('textos/', views.textos, name='textos'),
    path('textos/novo/', views.texto_editar, name='texto_editar'),
    path('textos/<int:pk>/editar/', views.texto_editar, name='texto_editar_pk'),
    path('campanhas/', views.campanhas, name='campanhas'),
    path('campanhas/<int:pk>/', views.campanha_detalhe, name='campanha_detalhe'),
    path('campanhas/<int:pk>/acao/', views.campanha_acao, name='campanha_acao'),
    path('historico/', views.historico, name='historico'),
    path('whatsapp/', views.whatsapp, name='whatsapp'),
    path('whatsapp/qr/', views.qr, name='qr'),
    path('whatsapp/conectar/', views.conectar, name='conectar'),
    path('tags/', views.tags, name='tags'),
    path('regras/', views.regras, name='regras'),
    path('tentativas/<int:pk>/reenviar/', views.reenvio, name='reenvio'),
]
