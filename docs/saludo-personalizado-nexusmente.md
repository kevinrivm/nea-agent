# Nea — saludo comercial personalizado

Se implementa el nombre de trato con procedencia en el contexto del CRM, sin
modificar el contrato normalizado de despacho, el webhook de Meta, las claves
ni la infraestructura. Si el CRM conoce un nombre humano fiable, el primer
saludo puede dirigirse por ese nombre. Si no, el agente saluda sin nombre.

Reglas:
- `contact.ficha.nombre_preferido` (declarado explícitamente por el lead) gana
  sobre `nombre_completo`, `nombre` y `contact.name` del perfil.
- Se evitan handles, números telefónicos, identificadores técnicos y nombres
  de negocios como apelativos.
- El primer turno prioriza contestar la pregunta comercial del cliente antes
  de hacer una nueva pregunta.
- Los turnos siguientes no reiteran saludos ni el nombre mecánicamente.
- Cuando el lead indique cómo desea que lo llamen, Nea tiene a su disposición
  `update_ficha(nombre_preferido=...)` para respetarlo en el futuro.
- El código agrega instrucciones y tests deterministas; la evaluación final
  del comportamiento del modelo requiere un test conversacional controlado.

Ejemplo esperado (cuando la base de conocimiento confirma la oferta):
Lead: «Hola Nexus, ¿qué hacen ustedes?»
Nea: «¡Hola, Daniel! Ayudamos a negocios a organizar su atención y sus
oportunidades comerciales con IA. ¿En qué tipo de negocio trabajas?»

No incluye garantía de conversión ni se inventa oferta comercial. No despliega.
