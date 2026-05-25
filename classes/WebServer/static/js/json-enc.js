/*
 * Description: Provides browser-side behavior for the json-enc web UI asset.
 * File: json-enc.js
 *
 * Copyright 2026 Kevin Burke
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://apache.org
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

(function(){
  var api;
  htmx.defineExtension('json-enc', {
    onEvent: function(name, evt) {
      if (name === "htmx:configRequest") {
        evt.detail.headers['Content-Type'] = 'application/json';
      }
    },
    encodeParameters: function(xhr, parameters, elt) {
      xhr.overrideMimeType('text/json');
      return (JSON.stringify(parameters));
    }
  });
})();